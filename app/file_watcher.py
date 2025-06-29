#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from feed_generator import FeedGenerator, FileIO
import threading
from typing import Dict
import stat

class MusicFileHandler(FileSystemEventHandler):
    """音楽ファイルの変更を監視するハンドラー"""
    
    def __init__(self):
        super().__init__()
        self.music_extensions = ['.mp3', '.m4a']
        # 処理の重複を避けるためのロック
        self.lock = threading.Lock()
        # ファイルサイズ監視用の辞書
        self.file_sizes: Dict[str, int] = {}
        # 処理済みファイルの記録
        self.processed_files: set = set()
        
    def is_music_file(self, file_path: str) -> bool:
        """音楽ファイルかどうか判定"""
        return any(file_path.lower().endswith(ext) for ext in self.music_extensions)
    
    def is_file_write_complete(self, file_path: str, max_attempts: int = 5, wait_time: float = 1.0) -> bool:
        """ファイルの書き込みが完了しているかチェック"""
        if not os.path.exists(file_path):
            return False
            
        for attempt in range(max_attempts):
            try:
                # ファイルサイズをチェック
                current_size = os.path.getsize(file_path)
                
                # 初回または前回と同じサイズの場合
                if file_path not in self.file_sizes:
                    self.file_sizes[file_path] = current_size
                    time.sleep(wait_time)
                    continue
                    
                # サイズが変わっていない場合
                if self.file_sizes[file_path] == current_size:
                    # ファイルが開けるかテスト
                    try:
                        with open(file_path, 'rb') as f:
                            # ファイルの先頭と末尾を読み取ってアクセス可能か確認
                            f.read(1024)
                            if current_size > 1024:
                                f.seek(-1024, 2)
                                f.read(1024)
                        
                        # ファイルのロック状態をチェック（Unix系）
                        if hasattr(os, 'access') and not os.access(file_path, os.W_OK):
                            time.sleep(wait_time)
                            continue
                            
                        return True
                    except (IOError, OSError):
                        # ファイルがまだロックされている
                        time.sleep(wait_time)
                        continue
                else:
                    # サイズが変わった場合は更新
                    self.file_sizes[file_path] = current_size
                    time.sleep(wait_time)
                    
            except (IOError, OSError):
                time.sleep(wait_time)
                continue
                
        return False
    
    def cleanup_file_tracking(self, file_path: str):
        """ファイル追跡情報をクリーンアップ"""
        if file_path in self.file_sizes:
            del self.file_sizes[file_path]
    
    def on_created(self, event):
        """ファイルが作成された時の処理"""
        if not event.is_directory and self.is_music_file(event.src_path):
            # バックグラウンドで処理するためのスレッドを起動
            thread = threading.Thread(target=self._handle_file_created, args=(event.src_path,))
            thread.daemon = True
            thread.start()
    
    def on_modified(self, event):
        """ファイルが変更された時の処理（書き込み完了を検知するため）"""
        if not event.is_directory and self.is_music_file(event.src_path):
            # 作成イベントと重複しないように処理済みファイルかチェック
            if event.src_path not in self.processed_files:
                thread = threading.Thread(target=self._handle_file_created, args=(event.src_path,))
                thread.daemon = True
                thread.start()
    
    def on_deleted(self, event):
        """ファイルが削除された時の処理"""
        if not event.is_directory and self.is_music_file(event.src_path):
            with self.lock:
                print(f"[file_watcher] File deleted: {event.src_path}")
                # 追跡情報をクリーンアップ
                self.cleanup_file_tracking(event.src_path)
                self.processed_files.discard(event.src_path)
                FeedGenerator.remove_music_file(event.src_path)
    
    def _handle_file_created(self, file_path: str):
        """ファイル作成処理を別スレッドで実行"""
        try:
            print(f"[file_watcher] Detected file: {file_path}, waiting for write completion...")
            
            # ファイルの書き込み完了を待つ
            if self.is_file_write_complete(file_path):
                with self.lock:
                    # 重複処理を避ける
                    if file_path in self.processed_files:
                        return
                    
                    self.processed_files.add(file_path)
                    print(f"[file_watcher] File write completed: {file_path}")
                    FeedGenerator.add_music_file(file_path)
                    # 追跡情報をクリーンアップ
                    self.cleanup_file_tracking(file_path)
            else:
                print(f"[file_watcher] File write completion timeout: {file_path}")
                self.cleanup_file_tracking(file_path)
                
        except Exception as e:
            print(f"[file_watcher] Error handling file {file_path}: {e}")
            self.cleanup_file_tracking(file_path)

class FileWatcher:
    """ファイル監視システム（イベントベース + ポーリング）"""
    
    def __init__(self, watch_directory: str, polling_interval: int = 30):
        self.watch_directory = watch_directory
        self.observer = Observer()
        self.handler = MusicFileHandler()
        self.polling_interval = polling_interval
        self.known_files = set()
        
    def scan_for_new_files(self):
        """ポーリングによる新しいファイルの検索"""
        try:
            current_files = set()
            for extension in ['.mp3', '.m4a']:
                import glob
                files = glob.glob(os.path.join(self.watch_directory, f"**/*{extension}"), recursive=True)
                current_files.update(files)
            
            # 新しいファイルをチェック
            new_files = current_files - self.known_files
            for new_file in new_files:
                print(f"[file_watcher] Polling detected new file: {new_file}")
                self.handler._handle_file_created(new_file)
            
            # 削除されたファイルをチェック
            deleted_files = self.known_files - current_files
            for deleted_file in deleted_files:
                print(f"[file_watcher] Polling detected deleted file: {deleted_file}")
                with self.handler.lock:
                    FeedGenerator.remove_music_file(deleted_file)
                    self.handler.cleanup_file_tracking(deleted_file)
                    self.handler.processed_files.discard(deleted_file)
            
            self.known_files = current_files
            
        except Exception as e:
            print(f"[file_watcher] Error in polling: {e}")
    
    def start(self):
        """監視を開始（イベントベース + ポーリング）"""
        print(f"[file_watcher] Starting file watcher for directory: {self.watch_directory}")
        print(f"[file_watcher] Polling interval: {self.polling_interval} seconds")
        
        # 初回スキャンで既存ファイルを記録
        self.scan_for_new_files()
        
        # イベントベース監視を開始
        self.observer.schedule(self.handler, self.watch_directory, recursive=True)
        self.observer.start()
        print(f"[file_watcher] File watcher started")
        
        try:
            while True:
                time.sleep(self.polling_interval)
                print(f"[file_watcher] Running periodic scan...")
                self.scan_for_new_files()
        except KeyboardInterrupt:
            self.stop()
    
    def stop(self):
        """監視を停止"""
        print(f"[file_watcher] Stopping file watcher")
        self.observer.stop()
        self.observer.join()
        print(f"[file_watcher] File watcher stopped")

if __name__ == "__main__":
    # 監視対象ディレクトリ
    watch_dir = FileIO.music_files_dir_path
    
    # 初回起動時にフィード全体を生成
    print("[file_watcher] Generating initial feeds...")
    FeedGenerator.generate()
    print("[file_watcher] Initial feeds generated")
    
    # ファイル監視を開始（本番用ポーリング間隔: 30秒）
    watcher = FileWatcher(watch_dir, polling_interval=30)
    watcher.start() 