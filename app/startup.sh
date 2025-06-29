#!/usr/bin/env bash

set -eu

cd /usr/local/apache2/htdocs/
if [ ! -e "music_files" ]; then
    ln -s /volumes/music_files music_files
fi

# ファイル監視システムをバックグラウンドで起動（ログを標準出力にリダイレクト）
python3 -B /usr/src/app/file_watcher.py 2>&1 &
httpd -D FOREGROUND
