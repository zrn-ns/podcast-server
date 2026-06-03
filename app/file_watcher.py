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

        # ポーラスレッドのみが触る状態。fullpath -> 前回観測した mtime。
        # ポーラが自前で観測値を持つことで、共有インデックスを跨スレッドで読まずに
        # 新規/削除/再タグ付け(mtimeの進み)を検知する(ワーカー単独保有の不変条件を維持)。
        self._seen_mtimes: Dict[str, float] = {}
        # ワーカースレッドのみが触る状態(ロック不要)
        self._size_cache: Dict[str, Tuple[int, float]] = {}
        self._defer_attempts: Dict[str, int] = {}
        # ワーカーが最後にループを一周した時刻(ハング検知用ハートビートの素)。
        # 別スレッドからは読むだけ(float の代入は GIL 下でアトミック)。
        self._worker_progress_at = time.time()

        self._worker = threading.Thread(target=self._run_worker, name="feed-worker", daemon=True)
        self._poller = threading.Thread(target=self._run_poller, name="feed-poller", daemon=True)

    # --- キュー投入 ---
    def _enqueue(self, path: str, kind: str):
        self._queue.put((path, kind))

    # --- ポーリング(取りこぼし補完) ---
    def _scan_once(self):
        # 現在のディスク上の音楽ファイルと mtime を取得
        current: Dict[str, float] = {}
        for extension in FileIO.music_extensions:
            pattern = os.path.join(self.watch_directory, f"**/*{extension}")
            for path in glob.glob(pattern, recursive=True):
                try:
                    current[path] = os.path.getmtime(path)
                except OSError:
                    continue

        seen = self._seen_mtimes

        # 削除を検知
        for gone_path in set(seen) - set(current):
            logger.info(f"Polling detected deleted file: {gone_path}")
            self._enqueue(gone_path, DELETED)

        # 新規・内容変更(再タグ付け)を検知。ポーラ自前の観測 mtime と比較するため、
        # インデックス未登録(タグ欠落等)のファイルでも後からのタグ修正を検知できる。
        for path, mtime in current.items():
            previous = seen.get(path)
            if previous is None:
                logger.info(f"Polling detected new file: {path}")
                self._enqueue(path, CHANGED)
            elif mtime > previous + 1.0:  # 1秒の余裕で誤差を吸収
                logger.info(f"Polling detected modified file: {path}")
                self._enqueue(path, CHANGED)

        self._seen_mtimes = current

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
            # ループを一周するたびに進捗時刻を更新する。これがハートビートの素になり、
            # ワーカーが apply_batch 等で恒久ブロックすると進捗が止まり unhealthy 化する。
            self._worker_progress_at = time.time()
            if pending:
                timeout = max(0.0, min(self.debounce_seconds, deadline - time.monotonic()))
            else:
                # アイドル時も heartbeat_interval ごとに必ず一周し、生存を示す
                timeout = self.heartbeat_interval
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

        # ポーラの観測 mtime をインデックスの記録値で初期化し、起動直後の全件再処理を避ける
        # (スレッド起動前なのでインデックス読み取りは単一スレッドで安全)
        self._seen_mtimes = FeedGenerator.indexed_mtimes()

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
                # ワーカーが直近で進捗していればハートビートを更新する。ワーカーが
                # 生きていても apply_batch 等でハングして進捗が止まれば、ここで
                # 更新が止まり HEALTHCHECK が unhealthy を返す(ハング型の片肺検知)。
                if time.time() - self._worker_progress_at < self.heartbeat_interval * 3:
                    self._write_heartbeat()
                else:
                    logger.error("worker appears stalled; heartbeat is not refreshed")
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


def _heartbeat_during(stop_event, path, interval=10.0):
    """generate() のように時間がかかる起動処理の間、ハートビートを更新し続ける。

    曲数が多いと初回生成が長くかかり、その間ハートビートが古くなって HEALTHCHECK が
    誤って unhealthy 判定するのを防ぐ。
    """
    while not stop_event.wait(interval):
        try:
            with open(path, "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass


if __name__ == "__main__":
    watch_dir = FileIO.music_files_dir_path

    # 初回生成は曲数次第で時間がかかるため、その間もハートビートを更新しておく
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass
    _gen_stop = threading.Event()
    _gen_hb = threading.Thread(target=_heartbeat_during, args=(_gen_stop, HEARTBEAT_FILE),
                               name="startup-heartbeat", daemon=True)
    _gen_hb.start()

    logger.info("Generating initial feeds...")
    FeedGenerator.generate()
    logger.info("Initial feeds generated")

    _gen_stop.set()  # 起動用ハートビートを止め、以降はワーカー進捗ベースに切り替える

    # ファイル監視を開始(本番用ポーリング間隔: 30秒)
    watcher = FileWatcher(watch_dir, polling_interval=30)
    watcher.start()
