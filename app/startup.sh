#!/bin/sh

set -eu

python3 /usr/src/app/feed_generator.py
python3 /usr/src/app/scheduler.py &
httpd -D FOREGROUND
