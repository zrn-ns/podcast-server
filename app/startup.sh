#!/usr/bin/env bash

set -eu

cd /usr/local/apache2/htdocs/
if [ ! -e "music_files" ]; then
    ln -s /volumes/music_files music_files
fi

# シグナル(docker stop 等)による正常停止か、子の予期せぬ終了かを区別するフラグ
signaled=0

terminate() {
    echo "startup.sh: stopping child processes..."
    kill -TERM "$watcher_pid" "$httpd_pid" 2>/dev/null || true
}
on_signal() {
    signaled=1
    terminate
}
trap on_signal TERM INT

# ファイル監視システムをバックグラウンドで起動(ログは標準出力へ)
python3 -B /usr/src/app/file_watcher.py 2>&1 &
watcher_pid=$!

# httpd もバックグラウンドで起動し、両者を監視する
httpd -D FOREGROUND &
httpd_pid=$!

# 1秒ごとに両プロセスの生存とシグナル受信を確認する。
# sleep をバックグラウンドにして wait することで、シグナルが即座に割り込める
# (bash の wait -n のトラップ割り込み挙動に依存しない堅牢な方式)。
while [ "$signaled" -eq 0 ] \
      && kill -0 "$watcher_pid" 2>/dev/null \
      && kill -0 "$httpd_pid" 2>/dev/null; do
    sleep 1 &
    wait $! 2>/dev/null || true
done

if [ "$signaled" -eq 1 ]; then
    # docker stop 等による正常停止 → 終了コード 0
    echo "startup.sh: received stop signal; shutting down gracefully"
    wait 2>/dev/null || true
    exit 0
fi

# どちらかの子が予期せず終了した(クラッシュ等)→ もう一方も止めて異常終了(1)で抜ける
# (コンテナが停止し、restart ポリシーで再起動される。片肺運転=サイレント障害を防ぐ)
echo "startup.sh: a child process exited unexpectedly; shutting down the container"
terminate
wait 2>/dev/null || true
exit 1
