#!/usr/bin/env bash

set -eu

# venv の python を絶対パスで特定する(PATH/環境変数が引き継がれない環境でも確実に解決)
VENV_PYTHON="${VIRTUAL_ENV:-/opt/venv}/bin/python3"

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

# 子プロセスは setsid で制御端末から切り離して起動する。
# httpd は SIGWINCH を「グレースフル停止」の合図として解釈するため、コンテナに割り当て
# られた TTY のウィンドウサイズ変更(ターミナル接続/DSMのUI操作等)で送られる SIGWINCH を
# 受け取ると勝手に止まってしまう。新しいセッションに分離すれば TTY 由来の SIGWINCH/SIGHUP
# が届かなくなる。setsid は(呼び出し元が pgroup リーダでないため)fork せず PID を保つので
# $! でそのまま追跡・kill できる。

# ファイル監視システムを起動(ログは標準出力へ)。
# venv の python を絶対パスで呼ぶ。NAS 等の一部ランタイムはイメージの ENV PATH を
# 引き継がず、PATH 依存だと system python(watchdog 等が無い)に解決され起動失敗するため。
setsid "$VENV_PYTHON" -B /usr/src/app/file_watcher.py 2>&1 &
watcher_pid=$!

# httpd も setsid で起動し、両者を監視する
setsid httpd -D FOREGROUND &
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
