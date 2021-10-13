#!/usr/bin/env bash

set -eu

cd /usr/local/apache2/htdocs/
ln -s /volumes/music_files music_files
python3 -B /usr/src/app/feed_generator.py
python3 -B /usr/src/app/scheduler.py &
httpd -D FOREGROUND
