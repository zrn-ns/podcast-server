# podcast-server

特定のディレクトリ内の音楽ファイルからRSSフィードを自動生成してくれるPodcastサーバソフト。

## 特徴

- **リアルタイム更新**: ファイルの追加・削除を監視し、自動的にフィードを差分更新
- **高効率**: 変更があったアルバムのフィードのみを更新するため、大量のファイルでも高速動作
- **初回起動時の全体生成**: コンテナ起動時に全フィードを生成し、その後は差分更新で対応

<img width="640" alt="ss_feed_list" src="https://user-images.githubusercontent.com/5319256/136659235-f189cad4-e8e0-4225-a726-add6af52f5d0.png">
<img width="640" alt="ss_single_feed" src="https://user-images.githubusercontent.com/5319256/136659238-5739f6fb-e84b-497f-8ede-32406ba56d99.png">

## 使い方

### Dockerを使う場合

```sh
# mp3ファイルを特定のディレクトリ配下に配置する
# ※楽曲はアルバム名ごとにフィードが作成されるため、mp3ファイルにはID3タグを設定しておくこと
# ※再帰的にファイルを探すので、ディレクトリ階層を掘ってもok

% cd ~
% mkdir mp3files
% cp hoge.mp3 mp3files/

# Dockerイメージを落としてくる

% docker pull zrnns/podcast-server

# Dockerイメージを起動する
# - 先程mp3ファイルを集めたディレクトリを　/volumes/music_files　にマウントする
# - 配信のルートURLを APP_ROOT_URL に設定する（rssフィード内でmp3ファイルを参照する際必要となる）
# - ポートのマッピングを行う(任意のポート->80番ポート)

% docker run -it -v ~/mp3files/:/volumes/music_files -e APP_ROOT_URL=http://localhost:8080/ -p 8080:80 zrnns/podcast-server

# 初回起動時にフィードが生成されます。その後、ファイルが追加・削除されると自動的にフィードが更新されます。

% open http://localhost:8080/
```
