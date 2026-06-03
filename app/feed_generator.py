#!/usr/bin/env python3
import os
import pathlib
import logging
import eyed3
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import glob
from dataclasses import dataclass, field
from itertools import groupby
from jinja2 import Template, Environment, FileSystemLoader
from typing import Any, Dict, List, Optional
import hashlib
import time
import urllib.parse
import pickle
import tempfile

# ロガー設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# アプリのルートURL(例: http://hogehoge.local:80/)
app_root_url: str = os.environ["APP_ROOT_URL"]

JST = timezone(timedelta(hours=9))

@dataclass
class MusicInfo:
    fullpath: str = ""
    album_name: str = ""
    title: str = ""
    duration_seconds: float = 0.0
    absolute_url: str = ""
    file_size_bytes: int = 0
    created_timestamp: float = 0.0
    thumbnail_url: str = ""
    # ファイルの最終更新時刻。変更検知(再タグ付け)に使うだけで、フィード内容の
    # 同一性判定(__eq__)には含めない(compare=False)。
    mtime: float = field(default=0.0, compare=False)

    def md5(self) -> str:
        return hashlib.md5(((self.album_name or "") + (self.title or "")).encode()).hexdigest()

@dataclass
class FeedInfo:
    album_name: str

    def hash(self) -> str:
        return hashlib.md5(self.album_name.encode()).hexdigest()

    def url(self) -> str:
        hash = self.hash()
        return f"{FileIO.feeds_dir_url}{hash}.xml"

    def file_path(self) -> str:
        hash = self.hash()
        return f"{FileIO.output_xml_dir_path}{hash}.xml"

class FileIO:
    feeds_dir_name = "feeds"
    htdocs_dir_path = "/usr/local/apache2/htdocs/"
    music_files_dir_path = f"{htdocs_dir_path}music_files/"
    music_extensions: List[str] = [".mp3", ".m4a"]
    index_html_file_path = f"{htdocs_dir_path}index.html"
    output_xml_dir_path = f"{htdocs_dir_path}{feeds_dir_name}/"
    feeds_dir_url = f"{app_root_url}{feeds_dir_name}/"

    templates_dir_path = "/usr/src/app/templates/"
    index_html_template_filename = "index-template.html.j2"
    feed_template_filename = "feed-template.xml.j2"

    thumbnail_dir_name = "thumbs"
    thumbnail_dir_path = f"{htdocs_dir_path}{thumbnail_dir_name}/"
    default_thumbnail_url = f"{app_root_url}{thumbnail_dir_name}/music.png"
    
    # インデックスファイルのパス
    index_file_path = f"{htdocs_dir_path}music_index.pkl"

    @staticmethod
    def list_music_fullpaths() -> List[str]:
        """監視対象ディレクトリ配下の音楽ファイルのフルパス一覧を返す"""
        music_file_fullpaths: List[str] = []
        for extension in FileIO.music_extensions:
            pattern = os.path.join(FileIO.music_files_dir_path, f"**/*{extension}")
            music_file_fullpaths.extend(glob.glob(pattern, recursive=True))
        return music_file_fullpaths

    @staticmethod
    def get_music_list() -> List[MusicInfo]:
        """全音楽ファイルから MusicInfo の一覧を生成する(読めない/タグ不備は除外)"""
        music_info_list: List[MusicInfo] = []
        for fullpath in FileIO.list_music_fullpaths():
            music_info = FileIO.build_music_info(fullpath)
            if music_info is not None:
                music_info_list.append(music_info)
        return music_info_list

    @staticmethod
    def get_feed_xml_template() -> Template:
        #テンプレート読み込み
        env = Environment(loader=FileSystemLoader(FileIO.templates_dir_path, encoding="utf8"))
        return env.get_template(FileIO.feed_template_filename)

    @staticmethod
    def get_index_html_template() -> Template:
        #テンプレート読み込み
        env = Environment(loader=FileSystemLoader(FileIO.templates_dir_path, encoding="utf8"))
        return env.get_template(FileIO.index_html_template_filename)

    @staticmethod
    def _atomic_write_text(path: str, text: str):
        """テキストを原子的に書き出す(tempfile に書いて os.replace で差し替え)。

        httpd が同じ htdocs を常時配信しているため、直接上書きすると書き込み途中の
        空/不完全なファイルをクライアントが取得しうる。同一ディレクトリ内の一時ファイル
        経由で差し替えることで、常に完全なファイルだけが見えるようにする。
        """
        dir_path = os.path.dirname(path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            # mkstemp は 0600 で作るため、httpd(www-data 等)が配信できるよう 0644 にする
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    @staticmethod
    def output_feed_xml(xml_text: str, feed_info: FeedInfo):
        FileIO._atomic_write_text(feed_info.file_path(), xml_text)

    @staticmethod
    def output_index_html(html_text: str):
        FileIO._atomic_write_text(FileIO.index_html_file_path, html_text)

    @staticmethod
    def save_music_index(by_path: Dict[str, MusicInfo]):
        """インデックス(fullpath -> MusicInfo)を原子的に保存する。

        tempfile に書いてから os.replace で差し替えることで、書き込み途中の
        クラッシュによる pickle 破損を防ぐ。
        """
        dir_path = os.path.dirname(FileIO.index_file_path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                pickle.dump(by_path, f)
            os.replace(tmp_path, FileIO.index_file_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    @staticmethod
    def load_music_index() -> Dict[str, MusicInfo]:
        """インデックスを読み込む。旧形式(list)や破損時は安全にフォールバックする。"""
        try:
            with open(FileIO.index_file_path, "rb") as f:
                data = pickle.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"music_index.pkl の読み込みに失敗、空で再構築します: {e}")
            return {}

        # 旧形式(List[MusicInfo])との後方互換: dict へ変換する。
        # なお mtime など後から追加したフィールドは dataclass のクラス既定値が
        # フォールバックするため、旧バージョンの pickle でも 0.0 として読める。
        if isinstance(data, list):
            logger.info("旧形式(list)のインデックスを dict へ変換します")
            return {mi.fullpath: mi for mi in data}
        if isinstance(data, dict):
            return data
        logger.warning("未知の形式のインデックス、空で再構築します")
        return {}

    @staticmethod
    def _extract_thumbnail_url(music_info: "MusicInfo", tag_images) -> str:
        """ID3 の埋め込み画像を thumbs/ に保存し、その URL を返す。無ければデフォルト URL。"""
        for image in tag_images:
            extension = ""
            if image.mime_type in ["image/jpeg", "image/jpg"]:
                extension = "jpg"
            elif image.mime_type == "image/png":
                extension = "png"
            if extension:
                filename = f"{music_info.md5()}.{extension}"
                thumbnail_path = f"{FileIO.thumbnail_dir_path}{filename}"
                if not os.path.exists(thumbnail_path):
                    with open(thumbnail_path, "wb") as fo:
                        fo.write(image.image_data)
                return f"{app_root_url}{FileIO.thumbnail_dir_name}/{filename}"
        return FileIO.default_thumbnail_url

    @staticmethod
    def _resolve_created_timestamp(file, fullpath: str) -> float:
        """配信日時(pubDate)に使う安定したタイムスタンプを返す。

        ID3 のリリース日タグを最優先し、無ければファイルの最終更新時刻(getmtime)を使う。
        getctime はメタデータ変更で動いてしまい不安定なため使わない。
        """
        try:
            best_date = file.tag.getBestDate()
        except Exception:
            best_date = None
        if best_date is not None and getattr(best_date, "year", None):
            try:
                dt = datetime(
                    best_date.year,
                    best_date.month or 1,
                    best_date.day or 1,
                    best_date.hour or 0,
                    best_date.minute or 0,
                    best_date.second or 0,
                    tzinfo=JST,
                )
                return dt.timestamp()
            except (ValueError, TypeError):
                pass
        return os.path.getmtime(fullpath)

    @staticmethod
    def build_music_info(fullpath: str) -> Optional[MusicInfo]:
        """単一の音楽ファイルから MusicInfo を生成する。読めない/タグ不備なら None を返す。

        get_music_list(全件)と差分更新の両方から使う唯一の生成経路。
        """
        try:
            # ファイルの存在とアクセス可能性を確認
            if not os.path.exists(fullpath) or not os.access(fullpath, os.R_OK):
                logger.warning(f"{fullpath} was skipped (not accessible)")
                return None

            file = eyed3.load(fullpath)
            if file is None or file.tag is None:
                logger.warning(f"{fullpath} was skipped (no id3 tag)")
                return None
            if file.tag.album is None:
                logger.warning(f"{fullpath} was skipped (album is none)")
                return None

            relative_path_escaped = urllib.parse.quote(str(pathlib.Path(fullpath).relative_to(FileIO.htdocs_dir_path)))

            music_info = MusicInfo()
            music_info.fullpath = fullpath
            music_info.album_name = file.tag.album
            # title が無いファイルはファイル名で代替(None のままだと md5/ソートで TypeError になる)
            music_info.title = file.tag.title if file.tag.title else os.path.basename(fullpath)
            music_info.duration_seconds = file.info.time_secs if file.info else 0.0
            music_info.absolute_url = f"{app_root_url}{relative_path_escaped}"
            music_info.file_size_bytes = os.path.getsize(fullpath)
            music_info.created_timestamp = FileIO._resolve_created_timestamp(file, fullpath)
            music_info.mtime = os.path.getmtime(fullpath)
            music_info.thumbnail_url = FileIO._extract_thumbnail_url(music_info, file.tag.images)
            return music_info

        except Exception as e:
            logger.warning(f"Error reading file {fullpath}: {e}")
            return None


class MusicIndex:
    """音楽ファイルのメタデータをメモリ常駐で保持するインデックス。

    内部表現は fullpath -> MusicInfo の dict。重複チェック・削除を O(1) で行える。
    アルバム名 -> fullpath集合 の補助インデックスも持ち、アルバム単位の取得を高速化する。
    """

    def __init__(self, by_path: Optional[Dict[str, MusicInfo]] = None):
        self._by_path: Dict[str, MusicInfo] = {}
        self._album_paths: Dict[str, set] = {}
        if by_path:
            for music_info in by_path.values():
                self.upsert(music_info)

    # --- 変更系 ---
    def upsert(self, music_info: MusicInfo) -> Optional[str]:
        """追加または更新する。アルバムが変わった場合は旧アルバム名を返す(フィード更新用)。"""
        old = self._by_path.get(music_info.fullpath)
        old_album = None
        if old is not None and old.album_name != music_info.album_name:
            old_album = old.album_name
            old_paths = self._album_paths.get(old_album)
            if old_paths is not None:
                old_paths.discard(old.fullpath)
                if not old_paths:
                    del self._album_paths[old_album]
        self._by_path[music_info.fullpath] = music_info
        self._album_paths.setdefault(music_info.album_name, set()).add(music_info.fullpath)
        return old_album

    def remove(self, fullpath: str) -> Optional[MusicInfo]:
        """削除する。削除した MusicInfo を返す(存在しなければ None)。"""
        music_info = self._by_path.pop(fullpath, None)
        if music_info is not None:
            paths = self._album_paths.get(music_info.album_name)
            if paths is not None:
                paths.discard(fullpath)
                if not paths:
                    del self._album_paths[music_info.album_name]
        return music_info

    # --- 参照系 ---
    def contains(self, fullpath: str) -> bool:
        return fullpath in self._by_path

    def get(self, fullpath: str) -> Optional[MusicInfo]:
        return self._by_path.get(fullpath)

    def album_musics(self, album_name: str) -> List[MusicInfo]:
        paths = self._album_paths.get(album_name, set())
        return [self._by_path[p] for p in paths if p in self._by_path]

    def album_names(self) -> List[str]:
        return sorted(self._album_paths.keys())

    def all_paths(self) -> set:
        return set(self._by_path.keys())

    def mtimes(self) -> Dict[str, float]:
        """fullpath -> mtime のスナップショット。items() を一括で取るため
        途中で None を参照するレースが起きない。"""
        return {path: mi.mtime for path, mi in list(self._by_path.items())}

    def __len__(self) -> int:
        return len(self._by_path)

    # --- 永続化 ---
    def save(self):
        FileIO.save_music_index(self._by_path)

    @classmethod
    def load(cls) -> "MusicIndex":
        return cls(by_path=FileIO.load_music_index())


class TemplateRenderer:
    @staticmethod
    def render_feed_xml(feed_info: FeedInfo, music_info_list: List[MusicInfo]):
        # 空のフィードは描画しない(channel.thumbnail_url の IndexError 防止も兼ねる)
        if not music_info_list:
            return

        items: List[Dict[str, Any]] = []

        for music_info in music_info_list:
            items.append({
                "title": music_info.title,
                "date_text_rfc1123": format_datetime(datetime.fromtimestamp(music_info.created_timestamp, JST)),
                "md5": music_info.md5(),
                "duration_hhmmss": time.strftime('%H:%M:%S', time.gmtime(music_info.duration_seconds)),
                "url": music_info.absolute_url,
                "file_size_bytes": music_info.file_size_bytes,
                "thumbnail_url": music_info.thumbnail_url
            })

        rendering_params = {
            "channel": {
              "title": feed_info.album_name,
              "thumbnail_url": music_info_list[0].thumbnail_url
            },
            "items": items
          }

        xml = FileIO.get_feed_xml_template().render(rendering_params)
        FileIO.output_feed_xml(xml, feed_info)

    @staticmethod
    def render_index_html(feed_info_list: List[FeedInfo]):
        feeds: List[Dict[str, Any]] = []
        for feed_info in feed_info_list:
            feeds.append({
              "path": feed_info.url(),
              "title": feed_info.album_name
            })

        rendering_params = { "last_update_date": datetime.now(JST), "feeds": feeds }

        html = FileIO.get_index_html_template().render(rendering_params)
        FileIO.output_index_html(html)

class FeedGenerator:
    # メモリ常駐のインデックス(長時間稼働する file_watcher プロセスが保持する)
    _index: Optional[MusicIndex] = None

    @classmethod
    def _get_index(cls) -> MusicIndex:
        """メモリ上のインデックスを返す。未ロードなら一度だけディスクから復元する。"""
        if cls._index is None:
            cls._index = MusicIndex.load()
        return cls._index

    @classmethod
    def generate(cls):
        """初回起動時の全体生成。インデックスをメモリ上に構築して保存する。"""
        index = MusicIndex()
        for music_info in FileIO.get_music_list():
            index.upsert(music_info)
        cls._index = index
        index.save()
        cls._regenerate_all_feeds(index)

    @classmethod
    def indexed_mtimes(cls) -> Dict[str, float]:
        """fullpath -> 記録済み mtime のマップ(ポーラ起動時の観測初期化用)。

        起動時(スレッド起動前)に単一スレッドから呼ばれる前提。items() を一括で
        スナップショットして None 参照のレースを避ける。
        """
        index = cls._get_index()
        return index.mtimes()

    @classmethod
    def add_music_file(cls, file_path: str):
        """単一ファイルの追加(apply_batch への薄いラッパ)。"""
        logger.info(f"Adding music file: {file_path}")
        cls.apply_batch(upserts=[file_path], removes=[])

    @classmethod
    def remove_music_file(cls, file_path: str):
        """単一ファイルの削除(apply_batch への薄いラッパ)。"""
        logger.info(f"Removing music file: {file_path}")
        cls.apply_batch(upserts=[], removes=[file_path])

    @classmethod
    def apply_batch(cls, upserts: List[str], removes: List[str]):
        """複数の追加/更新/削除を1回のバッチでまとめて反映する。

        インデックス保存と index.html 再生成はバッチ全体で1回だけ行うため、
        N 件の一括投入でも O(N) で済む(従来の per-file 保存による O(N^2) を解消)。
        影響を受けたアルバムのフィードのみ再生成する。
        """
        index = cls._get_index()
        affected_albums = set()
        index_dirty = False  # 保存が必要か(mtime同期のみでも True)

        # 削除を先に処理(同一バッチ内で削除→再追加が来た場合に追加が勝つ)
        for path in removes:
            removed = index.remove(path)
            if removed is not None:
                affected_albums.add(removed.album_name)
                index_dirty = True

        for path in upserts:
            new_music_info = FileIO.build_music_info(path)
            if new_music_info is None:
                logger.warning(f"{path} was skipped (invalid music file)")
                continue
            existing = index.get(path)
            if existing is not None:
                # 配信日時(pubDate)は一度確定したら保持する(再タグ付けで日時を揺らさない)
                new_music_info.created_timestamp = existing.created_timestamp
            if existing == new_music_info:
                # フィード内容に変化なし。mtime だけ同期してポーリングの再検知を止める
                if existing is not None and existing.mtime != new_music_info.mtime:
                    existing.mtime = new_music_info.mtime
                    index_dirty = True
                continue
            old_album = index.upsert(new_music_info)
            if old_album:
                affected_albums.add(old_album)  # 再タグ付けでアルバムが変わった場合の旧側
            affected_albums.add(new_music_info.album_name)
            index_dirty = True

        if not index_dirty:
            return

        # 保存と index.html 再生成はバッチで1回だけ
        index.save()
        if affected_albums:
            for album_name in affected_albums:
                cls._update_album_feed(album_name, index)
            cls._render_index(index)

    @classmethod
    def _regenerate_all_feeds(cls, index: MusicIndex):
        """全フィードと index.html を再生成する"""
        all_feeds: List[FeedInfo] = []
        for album_name in index.album_names():
            feed = FeedInfo(album_name=album_name)
            all_feeds.append(feed)
            sorted_music_list = sorted(index.album_musics(album_name), key=lambda e: e.title, reverse=True)
            TemplateRenderer.render_feed_xml(feed, sorted_music_list)
        TemplateRenderer.render_index_html(all_feeds)

    @classmethod
    def _update_album_feed(cls, album_name: str, index: MusicIndex):
        """特定のアルバムのフィードのみ更新。曲が無くなったら孤立フィードXMLを削除する。"""
        album_music_list = index.album_musics(album_name)
        feed = FeedInfo(album_name=album_name)
        if album_music_list:
            sorted_music_list = sorted(album_music_list, key=lambda e: e.title, reverse=True)
            TemplateRenderer.render_feed_xml(feed, sorted_music_list)
        else:
            # アルバム最後の曲が削除された → 残存するフィードXMLを削除(無ければ無視)
            pathlib.Path(feed.file_path()).unlink(missing_ok=True)
            logger.info(f"Removed orphaned feed for empty album: {album_name}")

    @classmethod
    def _render_index(cls, index: MusicIndex):
        """index.html を現在のアルバム一覧から再生成する"""
        all_feeds = [FeedInfo(album_name=name) for name in index.album_names()]
        TemplateRenderer.render_index_html(all_feeds)


if __name__ == "__main__":
    FeedGenerator.generate()
