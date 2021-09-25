#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import pathlib
import yaml
import logging
import eyed3
from datetime import datetime
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
from typing import List, Dict

@dataclass
class MusicInfo:
    fullpath: str = ""
    album_name: str = ""
    title: str = ""
    duration_seconds: int = ""
    relative_path_to_htdocs: str = ""
    file_size_bytes: int = 0
    created_timestamp: int = 0

    def md5(self) -> str:
        return hashlib.md5((self.album_name + self.title).encode()).hexdigest()

class FileIO:
    htdocs_dir_path = "/usr/local/apache2/htdocs/"
    music_files_dir_path = htdocs_dir_path + "music_files/"
    music_extensions: List[str] = ["mp3", "m4a"]
    index_html_file_path = htdocs_dir_path + "index.html"
    output_xml_dir_path = htdocs_dir_path + "feeds/"

    templates_dir_path = "/usr/src/app/templates/"
    index_html_template_filename = "index-template.html.j2"
    feed_template_filename = "feed-template.xml.j2"

    @staticmethod
    def get_music_list() -> List[MusicInfo]:
        # ファイルのフルパスの一覧を生成
        music_file_fullpaths: List[str] = []
        for extension in FileIO.music_extensions:
            music_file_fullpaths.extend(glob.glob(FileIO.music_files_dir_path + "/**/*" + extension))

        # フルパスの一覧からMusicInfoのリストを生成
        music_info_list: List[MusicInfo] = []
        for fullpath in music_file_fullpaths:
            # ファイルごとのメタデータを取得して、MusicInfoに情報を追加
            file = eyed3.load(fullpath)
            relative_path_to_htdocs = pathlib.Path(fullpath).relative_to(FileIO.htdocs_dir_path)
            file_size_bytes = os.path.getsize(fullpath)

            music_info = MusicInfo()
            music_info.fullpath = fullpath
            music_info.album_name = file.tag.album
            music_info.title = file.tag.title
            music_info.duration_seconds = file.info.time_secs
            music_info.relative_path_to_htdocs = relative_path_to_htdocs
            music_info.file_size_bytes = file_size_bytes
            music_info.created_timestamp = os.path.getctime(fullpath)
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
    def output_feed_xml(xml_text: str, album_name: str):
        xml_file_path = FileIO.output_xml_dir_path + album_name + ".xml"
        with open(xml_file_path, "w") as f:
            f.write(xml_text)

    @staticmethod
    def output_index_html(html_text: str):
        html_file_path = FileIO.index_html_file_path
        with open(html_file_path, "w") as f:
            f.write(html_text)


class TemplateRenderer:
    @staticmethod
    def render_feed_xml(album_name: str, music_info_list: [MusicInfo]):
        items: List[Dict[str: Any]] = []

        for music_info in music_info_list:
            items.append({
                "title": music_info.title,
                "date_text_rfc1123": format_date_time(music_info.created_timestamp),
                "md5": music_info.md5(),
                "duration_hhmmss": time.strftime('%H:%M:%S', time.gmtime(music_info.duration_seconds)),
                "url": "../" + music_info.relative_path_to_htdocs,
                "file_size_bytes": music_info.file_size_bytes,
            })

        rendering_params = {
            "channel": {
              "title": album_name
            },
            "items": items
          }

        xml = FileIO.get_feed_xml_template().render(rendering_params)
        FileIO.output_feed_xml(xml, album_name)

    @staticmethod
    def render_index_html(album_names: List[str]):
        feeds: List[Dict[str: Any]] = []
        for album_name in album_names:
            feeds.append({
              "path": "feeds/" + album_name + ".xml",
              "title": album_name
            })

        rendering_params = { "feeds": feeds }

        html = FileIO.get_index_html_template().render(rendering_params)
        FileIO.output_index_html(html)

class FeedGenerator:
    @staticmethod
    def generate():
        music_list = FileIO.get_music_list()
        music_list_grouped_by_album_name = groupby(sorted(music_list, key=lambda e: e.album_name), key=lambda e: e.album_name)
        template = FileIO.get_feed_xml_template()

        all_albums: str = []
        for key, music_list in music_list_grouped_by_album_name:
            all_albums.append(key)
            sorted_music_list: List[MusicInfo] = sorted(list(music_list), key=lambda e: e.title, reverse=True)
            TemplateRenderer.render_feed_xml(key, sorted_music_list)

        TemplateRenderer.render_index_html(all_albums)

if __name__ == "__main__":
    FeedGenerator.generate()
