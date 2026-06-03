# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

特定のディレクトリ内の音楽ファイル（mp3 / m4a）から、アルバム単位でPodcast用RSSフィードを自動生成するサーバ。Apache（httpd）が静的配信を担い、Python製のファイル監視デーモンがフィードXMLとインデックスHTMLを生成・更新する。配布はDocker Hub（`zrnns/podcast-server`）経由。

## アーキテクチャ

このアプリは「**1コンテナ内で2プロセスを並走させる**」構成が肝。`app/startup.sh` が次の2つを起動する:

1. **httpd（フォアグラウンド）** — `/usr/local/apache2/htdocs/` を80番ポートで静的配信。生成済みのフィードXML・サムネイル・mp3本体・index.htmlをそのまま返す。
2. **`file_watcher.py`（バックグラウンド）** — 音楽ファイルの追加・削除を監視し、フィードを差分更新する。

startup.sh はマウントされた `/volumes/music_files` を htdocs 配下に `music_files` としてシンボリックリンクする。つまり**音楽ファイル自体もhttpd経由で直接配信され、RSS内の `<enclosure>` URLはそのファイルを指す**。

### 主要モジュール

- **`app/feed_generator.py`** — フィード生成のコアロジック。クラス構成:
  - `MusicInfo` / `FeedInfo` — データクラス。`FeedInfo` はアルバム名のMD5ハッシュをファイル名・URLに使う（`<hash>.xml`）。`MusicInfo.md5()` はアルバム名+タイトルのハッシュでサムネイルファイル名に使う。
  - `FileIO` — 全パス定数・ファイル入出力・ID3タグ読み取り（eyeD3）・サムネイル抽出・**pickleによるインデックス永続化**（`music_index.pkl`）を集約。パスは全てここで定義されているので変更時はここを見る。
  - `TemplateRenderer` — Jinja2でXML/HTMLをレンダリング。
  - `FeedGenerator` — エントリポイント。`generate()`（全体生成）、`add_music_file()` / `remove_music_file()`（差分更新）。
- **`app/file_watcher.py`** — `feed_generator` を呼び出す監視層。
  - `MusicFileHandler` — watchdogのイベントハンドラ。ファイル**書き込み完了の検知**（`is_file_write_complete`: サイズが安定し読み取り可能になるまで待つ）が重要。コピー途中の不完全なファイルを処理しないための仕組み。各処理は別スレッドで走り、`threading.Lock` と `processed_files` で重複処理を防ぐ。
  - `FileWatcher` — **イベントベース監視（watchdog Observer）とポーリング（デフォルト30秒）の二重構成**。macOSのDockerボリュームではinotifyイベントが届かない問題があるため、ポーリングがフォールバックとして必須（直近の修正履歴の中心トピック）。

### データフロー

```
mp3/m4a 追加
  → file_watcher が検知（イベント or ポーリング）
  → 書き込み完了を待つ
  → FeedGenerator.add_music_file()
    → ID3タグからMusicInfo生成・サムネイル抽出
    → music_index.pkl に追記
    → 該当アルバムのフィードXMLのみ再生成（差分更新）
    → index.html を再生成
  → httpd が配信
```

初回起動時のみ `FeedGenerator.generate()` が全ファイルを走査し全フィードを生成。以降は差分更新。

### 重要な前提・制約

- **ID3タグの `album` が必須**。これが無いファイルはスキップされる（アルバム名がフィードの単位のため）。
- 環境変数 `APP_ROOT_URL` が必須。RSS内のmp3・サムネイルへの絶対URL生成に使われる（コンテナ内部では `feed_generator.py` 読み込み時に `os.environ` から取得し、未設定だと起動失敗する）。
- インデックスは `music_index.pkl`（pickle）で永続化。スキーマ（`MusicInfo`のフィールド）を変更すると既存pickleと非互換になる点に注意。
- 生成物（feeds配下のxml, thumbs, index.html）は htdocs に直接書き出される。リポジトリ上の `htdocs/` には `.gitkeep` とデフォルトサムネイル `music.png` のみが含まれる。

## 開発・実行コマンド

ローカルで直接動かす想定のテスト/ビルドスクリプトは存在しない。動作確認はDocker経由が基本。

```sh
# ビルド & 起動（docker-compose: ~/podcast_music_files をマウント、http://localhost:8080）
docker compose up --build

# 単体でビルド
docker build -t podcast-server .

# 任意ディレクトリをマウントして起動
docker run -it \
  -v ~/mp3files/:/volumes/music_files \
  -e APP_ROOT_URL=http://localhost:8080/ \
  -p 8080:80 \
  podcast-server
```

Pythonスクリプト単体の実行（コンテナ内、デバッグ用）:

```sh
# 全フィード生成のみ
APP_ROOT_URL=http://localhost/ python3 app/feed_generator.py
# 監視デーモン（初回生成 → 監視ループ）
APP_ROOT_URL=http://localhost/ python3 app/file_watcher.py
```

依存は `app/requirements.txt`（eyeD3 / Jinja2 / watchdog / PyYAML）。

## デプロイ

`.github/workflows/` でDocker Hubへ自動push（マルチアーキ: arm/v7, arm64, amd64）:

- **`master` ブランチへのpush** → `:latest` タグを公開。
- **`v*` タグのpush** → `:latest` と `:<バージョン>` の両方を公開（例: `v1.2.3` → `:1.2.3`）。

リリース時はバージョンタグ（`v` プレフィックス）を打つ。
