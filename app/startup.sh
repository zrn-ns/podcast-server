#!/usr/bin/env bash

set -eu

cd /usr/local/apache2/htdocs/
if [ ! -e "music_files" ]; then
    ln -s /volumes/music_files music_files
fi

# 子プロセスの PID を保持し、シグナルを転送して綺麗に停止できるようにする
pids=()

terminate() {
    echo "startup.sh: stopping child processes..."
    for pid in "${pids[@]}"; do
        kill -TERM "$pid" 2>/dev/null || true
    done
}
trap terminate TERM INT

# ファイル監視システムをバックグラウンドで起動(ログは標準出力へ)
python3 -B /usr/src/app/file_watcher.py 2>&1 &
pids+=($!)

# httpd もバックグラウンドで起動し、両者を wait で監視する
httpd -D FOREGROUND &
pids+=($!)

# どちらか一方でも終了したら、もう一方も止めてスクリプトを抜ける
# (コンテナが停止し、restart ポリシーで再起動される。片肺運転=サイレント障害を防ぐ)
wait -n || true
echo "startup.sh: a child process exited; shutting down the container"
terminate
wait || true
exit 1
