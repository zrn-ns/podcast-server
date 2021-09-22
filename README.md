# podcast-server

指定されたディレクトリ内の楽曲ファイルをPodcastとして公開するサーバソフト。

## 開発用コマンド（あとでforce-pushして消す🔥）

```sh
# コンテナのビルド
$ docker build  -t podcast-server:0.0.1 .

# コンテナの起動
$ docker run -p 80:80 --name podcast-server -d podcast-server:0.0.1

# コンテナに入る
$ docker exec -it podcast-server /bin/bash

# すべてのコンテナを削除する💣
$ docker rm -f $(docker ps -aq)

