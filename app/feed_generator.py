#!/usr/bin/env python3
import os
import pathlib
import logging
import eyed3
from datetime import datetime, timedelta, timezone
from wsgiref.handlers import format_date_time
import glob
from dataclasses import dataclass
from itertools import groupby
from jinja2 import Template, Environment, FileSystemLoader
import json
from typing import Any
from email.utils import formatdate
import hashlib
import time
from typing import List, Dict, Optional
import urllib
import pickle

# ロガー設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# アプリのルートURL(例: http://hogehoge.local:80/)
app_root_url: str = os.environ["APP_ROOT_URL"]

timezone = timezone(timedelta(hours=9))

@dataclass
class MusicInfo:
    fullpath: str = ""
    album_name: str = ""
    title: str = ""
    duration_seconds: int = ""
    absolute_url: str = ""
    file_size_bytes: int = 0
    created_timestamp: int = 0
    thumbnail_url: str = ""

    def md5(self) -> str:
        return hashlib.md5((self.album_name + self.title).encode()).hexdigest()

@dataclass
class FeedInfo:
    album_name: str

    def hash(self) -> str:
        return hashlib.md5(self.album_name.encode()).hexdigest()

    def url(self) -> str:
        hash = self.hash()
        return FileIO.feeds_dir_url + hash + ".xml"

    def file_path(self) -> str:
        hash = self.hash()
        return FileIO.output_xml_dir_path + hash + ".xml"

class FileIO:
    feeds_dir_name = "feeds"
    htdocs_dir_path = "/usr/local/apache2/htdocs/"
    music_files_dir_path = htdocs_dir_path + "music_files/"
    music_extensions: List[str] = ["mp3", "m4a"]
    index_html_file_path = htdocs_dir_path + "index.html"
    output_xml_dir_path = htdocs_dir_path + feeds_dir_name + "/"
    feeds_dir_url = app_root_url + feeds_dir_name + "/"

    templates_dir_path = "/usr/src/app/templates/"
    index_html_template_filename = "index-template.html.j2"
    feed_template_filename = "feed-template.xml.j2"

    thumbnail_dir_name = "thumbs"
    thumbnail_dir_path = htdocs_dir_path + thumbnail_dir_name + "/"
    default_thumbnail_url = app_root_url + thumbnail_dir_name + "/music.png"
    
    # インデックスファイルのパス
    index_file_path = htdocs_dir_path + "music_index.pkl"

    @staticmethod
    def get_music_list() -> List[MusicInfo]:
        # ファイルのフルパスの一覧を生成
        music_file_fullpaths: List[str] = []
        for extension in FileIO.music_extensions:
            music_file_fullpaths.extend(glob.glob(FileIO.music_files_dir_path + "/**/*" + extension, recursive=True))

        # フルパスの一覧からMusicInfoのリストを生成
        music_info_list: List[MusicInfo] = []
        for fullpath in music_file_fullpaths:
            # ファイルごとのメタデータを取得して、MusicInfoに情報を追加
            file = eyed3.load(fullpath)
            relative_path_escaped = urllib.parse.quote(str(pathlib.Path(fullpath).relative_to(FileIO.htdocs_dir_path)))
            absolute_url = app_root_url + relative_path_escaped
            file_size_bytes = os.path.getsize(fullpath)

            if file is None:
                logger.warning(f"{fullpath} was skipped (id3 info is none)")
                continue

            if file.tag is None:
                logger.warning(f"{fullpath} was skipped (tag is none)")
                continue

            if file.tag.album is None:
                logger.warning(f"{fullpath} was skipped (album_name is none)")
                continue

            music_info = MusicInfo()
            music_info.fullpath = fullpath
            music_info.album_name = file.tag.album
            music_info.title = file.tag.title
            music_info.duration_seconds = file.info.time_secs
            music_info.absolute_url = absolute_url
            music_info.file_size_bytes = file_size_bytes
            music_info.created_timestamp = os.path.getctime(fullpath)

            # サムネイル画像を保存する
            thumbnail_url = ""
            for image in file.tag.images:
                extension = ""
                if image.mime_type in ["image/jpeg", "image/jpg"]:
                    extension = "jpg"
                if image.mime_type == "image/png":
                    extension = "png"
                if extension != "":
                    filename = music_info.md5() + "." + extension
                    thumbnail_path = FileIO.thumbnail_dir_path + filename
                    thumbnail_url = app_root_url + FileIO.thumbnail_dir_name + "/" + filename
                    if not os.path.exists(thumbnail_path):
                        with open(thumbnail_path, "wb") as fo:
                            fo.write(image.image_data)
                    break
            if thumbnail_url != "":
                music_info.thumbnail_url = thumbnail_url
            else:
                music_info.thumbnail_url = FileIO.default_thumbnail_url

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
    def output_feed_xml(xml_text: str, feed_info: FeedInfo):
        xml_file_path = feed_info.file_path()
        with open(xml_file_path, "w") as f:
            f.write(xml_text)

    @staticmethod
    def output_index_html(html_text: str):
        html_file_path = FileIO.index_html_file_path
        with open(html_file_path, "w") as f:
            f.write(html_text)

    @staticmethod
    def save_music_index(music_list: List[MusicInfo]):
        """音楽ファイルのインデックスを保存"""
        with open(FileIO.index_file_path, "wb") as f:
            pickle.dump(music_list, f)

    @staticmethod
    def load_music_index() -> List[MusicInfo]:
        """音楽ファイルのインデックスを読み込み"""
        try:
            with open(FileIO.index_file_path, "rb") as f:
                return pickle.load(f)
        except FileNotFoundError:
            return []

    @staticmethod
    def get_music_info_from_file(fullpath: str) -> Optional[MusicInfo]:
        """単一の音楽ファイルからMusicInfoを生成"""
        try:
            # ファイルの存在とアクセス可能性を確認
            if not os.path.exists(fullpath) or not os.access(fullpath, os.R_OK):
                logger.warning(f"{fullpath} is not accessible")
                return None
                
            file = eyed3.load(fullpath)
            relative_path_escaped = urllib.parse.quote(str(pathlib.Path(fullpath).relative_to(FileIO.htdocs_dir_path)))
            absolute_url = app_root_url + relative_path_escaped
            file_size_bytes = os.path.getsize(fullpath)

            if file is None or file.tag is None or file.tag.album is None:
                logger.warning(f"{fullpath} has no valid ID3 tag information")
                return None

            music_info = MusicInfo()
            music_info.fullpath = fullpath
            music_info.album_name = file.tag.album
            music_info.title = file.tag.title if file.tag.title else os.path.basename(fullpath)
            music_info.duration_seconds = file.info.time_secs
            music_info.absolute_url = absolute_url
            music_info.file_size_bytes = file_size_bytes
            music_info.created_timestamp = os.path.getctime(fullpath)

            # サムネイル画像を保存する
            thumbnail_url = ""
            for image in file.tag.images:
                extension = ""
                if image.mime_type in ["image/jpeg", "image/jpg"]:
                    extension = "jpg"
                if image.mime_type == "image/png":
                    extension = "png"
                if extension != "":
                    filename = music_info.md5() + "." + extension
                    thumbnail_path = FileIO.thumbnail_dir_path + filename
                    thumbnail_url = app_root_url + FileIO.thumbnail_dir_name + "/" + filename
                    if not os.path.exists(thumbnail_path):
                        with open(thumbnail_path, "wb") as fo:
                            fo.write(image.image_data)
                    break
            if thumbnail_url != "":
                music_info.thumbnail_url = thumbnail_url
            else:
                music_info.thumbnail_url = FileIO.default_thumbnail_url

            return music_info
            
        except Exception as e:
            logger.warning(f"Error reading file {fullpath}: {e}")
            return None


class TemplateRenderer:
    @staticmethod
    def render_feed_xml(feed_info: FeedInfo, music_info_list: List[MusicInfo]):
        items: List[Dict[str: Any]] = []

        for music_info in music_info_list:
            items.append({
                "title": music_info.title,
                "date_text_rfc1123": format_date_time(music_info.created_timestamp),
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
        feeds: List[Dict[str: Any]] = []
        for feed_info in feed_info_list:
            feeds.append({
              "path": feed_info.url(),
              "title": feed_info.album_name
            })

        rendering_params = { "last_update_date": datetime.now(timezone), "feeds": feeds }

        html = FileIO.get_index_html_template().render(rendering_params)
        FileIO.output_index_html(html)

class FeedGenerator:
    @staticmethod
    def generate():
        """初回起動時の全体生成"""
        music_list = FileIO.get_music_list()
        FileIO.save_music_index(music_list)
        FeedGenerator._regenerate_all_feeds(music_list)

    @staticmethod
    def add_music_file(file_path: str):
        """新しい音楽ファイルを追加し、フィードを差分更新"""
        logger.info(f"Adding music file: {file_path}")
        
        # 新しいファイルの情報を取得
        new_music_info = FileIO.get_music_info_from_file(file_path)
        if new_music_info is None:
            logger.warning(f"{file_path} was skipped (invalid music file)")
            return

        # 既存のインデックスを読み込み
        existing_music_list = FileIO.load_music_index()
        
        # 重複チェック
        for existing in existing_music_list:
            if existing.fullpath == file_path:
                logger.info(f"File already exists in index: {file_path}")
                return

        # インデックスに追加
        existing_music_list.append(new_music_info)
        FileIO.save_music_index(existing_music_list)

        # 該当アルバムのフィードのみ更新
        FeedGenerator._update_album_feed(new_music_info.album_name, existing_music_list)
        
        # インデックスページを更新
        all_feeds = FeedGenerator._get_all_feeds(existing_music_list)
        TemplateRenderer.render_index_html(all_feeds)

    @staticmethod
    def remove_music_file(file_path: str):
        """音楽ファイルを削除し、フィードを差分更新"""
        logger.info(f"Removing music file: {file_path}")
        
        # 既存のインデックスを読み込み
        existing_music_list = FileIO.load_music_index()
        
        # 削除対象を探す
        removed_music = None
        updated_music_list = []
        for music in existing_music_list:
            if music.fullpath == file_path:
                removed_music = music
            else:
                updated_music_list.append(music)

        if removed_music is None:
            logger.info(f"File not found in index: {file_path}")
            return

        # インデックスを更新
        FileIO.save_music_index(updated_music_list)

        # 該当アルバムのフィードを更新
        FeedGenerator._update_album_feed(removed_music.album_name, updated_music_list)
        
        # インデックスページを更新
        all_feeds = FeedGenerator._get_all_feeds(updated_music_list)
        TemplateRenderer.render_index_html(all_feeds)

    @staticmethod
    def _regenerate_all_feeds(music_list: List[MusicInfo]):
        """全フィードを再生成"""
        music_list_grouped_by_album_name = groupby(sorted(music_list, key=lambda e: e.album_name), key=lambda e: e.album_name)
        
        all_feeds: List[FeedInfo] = []
        for key, music_list in music_list_grouped_by_album_name:
            feed = FeedInfo(album_name=key)
            all_feeds.append(feed)
            sorted_music_list: List[MusicInfo] = sorted(list(music_list), key=lambda e: e.title, reverse=True)
            TemplateRenderer.render_feed_xml(feed, sorted_music_list)

        TemplateRenderer.render_index_html(all_feeds)

    @staticmethod
    def _update_album_feed(album_name: str, music_list: List[MusicInfo]):
        """特定のアルバムのフィードのみ更新"""
        album_music_list = [music for music in music_list if music.album_name == album_name]
        if album_music_list:
            feed = FeedInfo(album_name=album_name)
            sorted_music_list: List[MusicInfo] = sorted(album_music_list, key=lambda e: e.title, reverse=True)
            TemplateRenderer.render_feed_xml(feed, sorted_music_list)

    @staticmethod
    def _get_all_feeds(music_list: List[MusicInfo]) -> List[FeedInfo]:
        """全フィード情報を取得"""
        album_names = list(set([music.album_name for music in music_list]))
        return [FeedInfo(album_name=name) for name in album_names]

if __name__ == "__main__":
    FeedGenerator.generate()
