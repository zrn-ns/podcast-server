# podcast-server

特定のディレクトリ内のmp3ファイルからフィードを自動生成してくれるPodcastサーバソフト。

<img width="640" alt="ss_feed_list" src="https://user-images.githubusercontent.com/5319256/136659235-f189cad4-e8e0-4225-a726-add6af52f5d0.png">
<img width="640" alt="ss_single_feed" src="https://user-images.githubusercontent.com/5319256/136659238-5739f6fb-e84b-497f-8ede-32406ba56d99.png">

## 使い方

### Dockerを使う場合

```sh
# mp3ファイルを特定のディレクトリ配下に配置する
# ※楽曲はアルバム名ごとにフィードが作成されるため、mp3ファイルにはID3タグを設定しておくこと
# ※再帰的にファイルを探すので、ディレクトリ階層を掘ってもok
$ cd ~
$ mkdir mp3files
$ cp hoge.mp3 mp3files/

# Dockerイメージを落としてくる
$ docker pull zrnns/podcast-server

# Dockerイメージを起動する
# - 先程mp3ファイルを集めたディレクトリを　/volumes/music_files　にマウントする
# - 配信のルートURLを APP_ROOT_URL に設定する（rssフィード内でmp3ファイルを参照する際必要となる）
# - RSSフィードの生成の頻度を CRONTAB_SETTING_TEXT に設定する
# - ポートのマッピングを行う(任意のポート->80番ポート)
$ docker run -d -v ~/mp3files/:/volumes/music_files -e APP_ROOT_URL=http://localhost:8080/ -e CRONTAB_SETTING_TEXT="*/15 * * * *" -p 8080:80 zrnns/podcast-server

# フィードが生成されるまで待ってから（数秒~数分程度）待ってから、ブラウザ等でlocalhost:8080 にアクセスすると、フィードが生成されている。
$ open http://localhost:8080/
```
