#!/usr/bin/env bash

set -eu

ln -s /volumes/music_files /usr/local/apache2/htdocs/music_files
python3 -B /usr/src/app/feed_generator.py
python3 -B /usr/src/app/scheduler.py &
httpd -D FOREGROUND
