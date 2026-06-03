#!/usr/bin/env python3

import os
import time
import glob
import queue
import logging
import threading
from typing import Dict, Tuple

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from feed_generator import FeedGenerator, FileIO

# ロガー設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# キューに積むイベントの種別
CHANGED = "changed"   # 追加 or 変更
DELETED = "deleted"   # 削除
_SHUTDOWN = object()   # ワーカー停止用センチネル

# 死活監視用ハートビートファイル(HEALTHCHECK が鮮度を見る)
HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "/tmp/podcast_watcher_heartbeat")


def is_music_file(path: str) -> bool:
    """音楽ファイル(拡張子一致)かどうか判定する"""
    return any(path.lower().endswith(ext) for ext in FileIO.music_extensions)


class _EnqueueHandler(FileSystemEventHandler):
    """watchdog のイベントをキューに積むだけの軽量ハンドラ。

    重い処理(タグ読み取り・フィード生成)は単一のワーカースレッドに集約するため、
    ここではイベント種別とパスをキューに渡すだけにする。
    """

    def __init__(self, enqueue):
        super().__init__()
        self._enqueue = enqueue

    def on_created(self, event):
        if not event.is_directory and is_music_file(event.src_path):
            self._enqueue(event.src_path, CHANGED)

    def on_modified(self, event):
        if not event.is_directory and is_music_file(event.src_path):
            self._enqueue(event.src_path, CHANGED)

    def on_moved(self, event):
        # リネーム/移動は「旧パス削除 + 新パス追加」として扱う
        if event.is_directory:
            return
        if is_music_file(event.src_path):
            self._enqueue(event.src_path, DELETED)
        dest_path = getattr(event, "dest_path", "")
        if dest_path and is_music_file(dest_path):
            self._enqueue(dest_path, CHANGED)

    def on_deleted(self, event):
        if not event.is_directory and is_music_file(event.src_path):
            self._enqueue(event.src_path, DELETED)


class FileWatcher:
    """ファイル監視システム(イベント監視 + ポーリングを単一ワーカーに集約)。

    - watchdog によるイベント監視: 変更を即時に検知する。
    - 定期ポーリング: watchdog のイベントが届かない環境(macOS の Docker ボリューム等)
      での取りこぼしを補完する。
    両経路とも「パスとイベント種別をキューに積むだけ」とし、単一のワーカースレッドが
    消費する。イベントは debounce_seconds 静止するまで貯めてから1回のバッチで反映する
    ため、大量ファイル投入時も「インデックス保存1回・index.html再生成1回」で済む。
    単一ワーカーなのでインデックスへの排他ロックは不要。
    """

    def __init__(self, watch_directory: str, polling_interval: int = 30,
                 debounce_seconds: float = 2.0, max_batch_seconds: float = 30.0,
                 max_defer_attempts: int = 30, heartbeat_interval: float = 10.0,
                 heartbeat_file: str = HEARTBEAT_FILE):
        self.watch_directory = watch_directory
        self.polling_interval = polling_interval
        self.debounce_seconds = debounce_seconds
        self.max_batch_seconds = max_batch_seconds
        self.max_defer_attempts = max_defer_attempts
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_file = heartbeat_file

        self._queue: "queue.Queue" = queue.Queue()
        self._observer = Observer()
        self._handler = _EnqueueHandler(self._enqueue)
        self._stop = threading.Event()

        # ポーラスレッドのみが触る状態
        self._known_files = set()
        # ワーカースレッドのみが触る状態(ロック不要)
        self._size_cache: Dict[str, Tuple[int, float]] = {}
        self._defer_attempts: Dict[str, int] = {}

        self._worker = threading.Thread(target=self._run_worker, name="feed-worker", daemon=True)
        self._poller = threading.Thread(target=self._run_poller, name="feed-poller", daemon=True)

    # --- キュー投入 ---
    def _enqueue(self, path: str, kind: str):
        self._queue.put((path, kind))

    # --- ポーリング(取りこぼし補完) ---
    def _scan_once(self):
        current = set()
        for extension in FileIO.music_extensions:
            pattern = os.path.join(self.watch_directory, f"**/*{extension}")
            current.update(glob.glob(pattern, recursive=True))

        for new_path in current - self._known_files:
            logger.info(f"Polling detected new file: {new_path}")
            self._enqueue(new_path, CHANGED)
        for gone_path in self._known_files - current:
            logger.info(f"Polling detected deleted file: {gone_path}")
            self._enqueue(gone_path, DELETED)

        # 既存ファイルの内容変更(再タグ付け)を mtime の進みで検知する。
        # watchdog のイベントが届かない環境(macOS の Docker ボリューム等)でも
        # タグ修正を反映できるようにするための経路。
        indexed_mtimes = FeedGenerator.indexed_mtimes()
        for path in current & self._known_files:
            stored = indexed_mtimes.get(path)
            if stored is None:
                continue
            try:
                disk_mtime = os.path.getmtime(path)
            except OSError:
                continue
            if disk_mtime > stored + 1.0:  # 1秒の余裕で誤差を吸収
                logger.info(f"Polling detected modified file: {path}")
                self._enqueue(path, CHANGED)

        self._known_files = current

    def _run_poller(self):
        # polling_interval ごとにスキャン(stop がセットされたら即終了)
        while not self._stop.wait(self.polling_interval):
            try:
                self._scan_once()
            except Exception as e:
                logger.error(f"Error in polling: {e}")

    # --- ワーカー(単一消費者) ---
    def _run_worker(self):
        pending: Dict[str, str] = {}
        deadline = None
        while True:
            if pending:
                timeout = max(0.0, min(self.debounce_seconds, deadline - time.monotonic()))
            else:
                timeout = None  # イベントが来るまでブロック
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                # debounce 静止 or 最大バッチ時間に到達 → まとめて反映
                if pending:
                    self._flush(pending)
                    pending = {}
                    deadline = None
                continue

            if item is _SHUTDOWN:
                if pending:
                    self._flush(pending)
                break

            path, kind = item
            pending[path] = kind  # 同一パスは最新のイベント種別が勝つ
            if deadline is None:
                deadline = time.monotonic() + self.max_batch_seconds
            elif time.monotonic() >= deadline:
                # イベントが途切れず流れ続けても最大バッチ時間で必ず吐き出す
                self._flush(pending)
                pending = {}
                deadline = None

    def _is_write_complete(self, path: str) -> bool:
        """ファイルの書き込みが完了していそうかを判定する。

        サイズと mtime が前回観測から変化していなければ完了とみなす。初回観測でも、
        最終更新から debounce_seconds 以上経過していれば完了とみなす(無駄な遅延回避)。
        """
        try:
            st = os.stat(path)
        except OSError:
            return False
        if st.st_size == 0:
            return False
        signature = (st.st_size, st.st_mtime)
        previous = self._size_cache.get(path)
        self._size_cache[path] = signature
        if previous == signature:
            return True
        if time.time() - st.st_mtime >= self.debounce_seconds:
            return True
        return False

    def _flush(self, pending: Dict[str, str]):
        upserts = []
        removes = []
        defer: Dict[str, str] = {}

        for path, kind in pending.items():
            if kind == DELETED:
                removes.append(path)
                self._size_cache.pop(path, None)
                self._defer_attempts.pop(path, None)
                continue
            # CHANGED
            if not os.path.exists(path):
                continue
            if self._is_write_complete(path):
                upserts.append(path)
                self._size_cache.pop(path, None)
                self._defer_attempts.pop(path, None)
            else:
                # まだ書き込み中 → 次のバッチで再評価(上限まで)
                attempts = self._defer_attempts.get(path, 0) + 1
                if attempts <= self.max_defer_attempts:
                    self._defer_attempts[path] = attempts
                    defer[path] = CHANGED
                else:
                    logger.warning(f"File write did not settle, giving up: {path}")
                    self._size_cache.pop(path, None)
                    self._defer_attempts.pop(path, None)

        try:
            if upserts or removes:
                logger.info(f"Applying batch: {len(upserts)} upsert(s), {len(removes)} remove(s)")
                FeedGenerator.apply_batch(upserts=upserts, removes=removes)
        except Exception as e:
            logger.error(f"Error applying batch: {e}")

        # 書き込み未完了分は少し待ってから再投入する(busy-loop 防止)
        if defer:
            def requeue():
                if not self._stop.wait(self.debounce_seconds):
                    for path, kind in defer.items():
                        self._enqueue(path, kind)
            threading.Thread(target=requeue, name="feed-requeue", daemon=True).start()

    def _write_heartbeat(self):
        """死活監視用に現在時刻をハートビートファイルへ書く"""
        try:
            with open(self.heartbeat_file, "w") as f:
                f.write(str(time.time()))
        except OSError as e:
            logger.warning(f"failed to write heartbeat: {e}")

    # --- ライフサイクル ---
    def start(self):
        logger.info(f"Starting file watcher for directory: {self.watch_directory}")
        logger.info(f"Polling interval: {self.polling_interval}s, debounce: {self.debounce_seconds}s")

        # 既知ファイルをインデックスのパス集合で初期化し、起動時の全件再処理を避ける
        self._known_files = FeedGenerator.indexed_paths()

        self._worker.start()
        self._observer.schedule(self._handler, self.watch_directory, recursive=True)
        self._observer.start()
        self._poller.start()
        self._write_heartbeat()
        logger.info("File watcher started")

        try:
            while not self._stop.is_set():
                # サブスレッドの死活監視: どれか落ちたら監視を畳んでプロセスを終了させ、
                # コンテナを再起動させる(片肺運転=サイレント障害を防ぐ)
                if not self._worker.is_alive():
                    logger.error("worker thread died; shutting down to trigger restart")
                    break
                if not self._poller.is_alive():
                    logger.error("poller thread died; shutting down to trigger restart")
                    break
                if not self._observer.is_alive():
                    logger.error("observer thread died; shutting down to trigger restart")
                    break
                self._write_heartbeat()
                time.sleep(self.heartbeat_interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        logger.info("Stopping file watcher")
        self._stop.set()
        try:
            self._observer.stop()
            self._observer.join(timeout=5)
        except Exception:
            pass
        # ワーカーに残りを吐き出させてから停止
        self._queue.put(_SHUTDOWN)
        self._worker.join(timeout=10)
        logger.info("File watcher stopped")


if __name__ == "__main__":
    watch_dir = FileIO.music_files_dir_path

    # 初回起動時にフィード全体を生成
    logger.info("Generating initial feeds...")
    FeedGenerator.generate()
    logger.info("Initial feeds generated")

    # ファイル監視を開始(本番用ポーリング間隔: 30秒)
    watcher = FileWatcher(watch_dir, polling_interval=30)
    watcher.start()
