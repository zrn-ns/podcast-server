FROM httpd:2.4

LABEL maintainer="zrn-ns"

# アプリのルートURLを引数として受け取る
ARG APP_ROOT_URL="http://localhost:80/"
ENV APP_ROOT_URL=$APP_ROOT_URL

# Python と venv をインストール(vim 等の不要パッケージは入れない)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-venv && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# venv を作成し、以降の python/pip を venv のものに固定する
# (システム Python を直接汚さないため --break-system-packages を排除)
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# 依存だけを先に COPY/install することで、アプリのソース変更時に
# pip install レイヤのキャッシュを再利用できるようにする
COPY app/requirements.txt /usr/src/app/requirements.txt
RUN pip install --no-cache-dir -r /usr/src/app/requirements.txt

# アプリ本体をコピー
COPY app/ /usr/src/app/

# 配信に必要なファイルをコピー
COPY htdocs /usr/local/apache2/htdocs

# tell the port number the container should expose
EXPOSE 80

# file_watcher のハートビートが新しいことを確認する(監視デーモンの片肺運転を検知)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import os, time, sys; hb = os.environ.get('HEARTBEAT_FILE', '/tmp/podcast_watcher_heartbeat'); sys.exit(0 if os.path.exists(hb) and (time.time() - os.path.getmtime(hb)) < 90 else 1)"

CMD ["/usr/src/app/startup.sh"]
