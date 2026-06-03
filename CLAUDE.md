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
  - `FileIO` — 全パス定数・ファイル入出力・ID3タグ読み取り（eyeD3, `build_music_info`）・サムネイル抽出・インデックスの**原子的**永続化（`music_index.pkl` を tempfile→`os.replace`）を集約。`_atomic_write_text` で feed XML / index.html も原子的に書く（httpd が書き込み途中を配信しないため）。パスは全てここで定義。
  - `MusicIndex` — メモリ常駐インデックス。内部は `Dict[fullpath, MusicInfo]` ＋ アルバム名→fullpath集合の補助辞書で、重複チェック・削除・アルバム取得を O(1) 化。
  - `TemplateRenderer` — Jinja2でXML/HTMLをレンダリング。pubDate は JST(+0900) 表記。
  - `FeedGenerator` — エントリポイント。`_index`（`MusicIndex`）をクラス変数でメモリ常駐させる。`generate()`（全体生成）、`apply_batch(upserts, removes)`（**まとめて反映**: 保存1回・影響アルバムのフィードと index.html を1回だけ再生成 → N件一括投入が O(N)）。`add_music_file`/`remove_music_file` は `apply_batch` の薄いラッパ。
- **`app/file_watcher.py`** — `feed_generator` を駆動する監視層。**単一ワーカースレッド + `queue.Queue` + デバウンス/バッチ**構成。
  - watchdogイベントとポーリングの両経路は「パスとイベント種別をキューに積むだけ」。重い処理は単一ワーカーに集約するためインデックスへのロックは不要。
  - ワーカーは `debounce_seconds` 静止するまでイベントを貯め、1回の `apply_batch` で反映（`max_batch_seconds` で上限）。`_is_write_complete` は size+mtime の安定で書き込み完了を判定（固定sleepとスレッド大量生成を廃止）。
  - ポーリング（既定30秒）は自前の `_seen_mtimes` で新規/削除/再タグ付け(mtimeの進み)を検知。**macOSのDockerボリュームではinotifyが届かないため、ポーリングがフォールバックとして必須**。
  - ハートビートファイルでワーカーの進捗を示し、`HEALTHCHECK` が片肺/ハングを検知。サブスレッド死亡時はプロセス終了→`restart`で復帰。

### データフロー

```
mp3/m4a 追加・変更・削除
  → watchdog or ポーリングが検知 → (path, kind) をキューに積む
  → 単一ワーカーが debounce_seconds 貯めて 1 バッチに集約
  → FeedGenerator.apply_batch(upserts, removes)
    → build_music_info で ID3→MusicInfo 生成・サムネ抽出（変化が無ければ no-op）
    → MusicIndex を更新（O(1)）→ music_index.pkl を原子的に1回保存
    → 影響アルバムのフィードXMLのみ再生成（空アルバムは孤立XMLを削除）
    → index.html を1回だけ再生成
  → httpd が配信
```

初回起動時のみ `FeedGenerator.generate()` が全ファイルを走査して `_index` を構築。以降は差分のバッチ反映。

### 重要な前提・制約

- **ID3タグの `album` が必須**。無いファイルはスキップされインデックスに載らない（後からタグ付与してもポーリングの mtime 検知で拾われる）。`title` 欠落時はファイル名で代替。
- 環境変数 `APP_ROOT_URL` が必須。RSS内のmp3・サムネイルへの絶対URL生成に使う（`feed_generator.py` 読み込み時に `os.environ` から取得、未設定だと起動失敗）。
- インデックスは `music_index.pkl`（pickle、`Dict[fullpath, MusicInfo]`）。`MusicInfo` に**フィールドを追加する場合は dataclass のクラス既定値を必ず持たせる**（旧pickleはその既定値でフォールバックして読める）。`mtime` は `compare=False`（フィード同一性判定に含めず変更検知のトリガにのみ使う）。pubDate(`created_timestamp`)は一度確定したら `apply_batch` で保持する。
- 生成物（feeds配下のxml, thumbs, index.html）は htdocs に書き出される。リポジトリ上の `htdocs/` には `.gitkeep` とデフォルトサムネイル `music.png` のみ。
- 配信されるファイルは world-readable(0644)である必要がある（`_atomic_write_text` は mkstemp の 0600 を 0644 に補正している）。
- PID1 は `startup.sh`(bash)。`STOPSIGNAL SIGTERM`（httpd継承の SIGWINCH を上書き）で `docker stop` がgracefulに効く。

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

依存は `app/requirements.txt`（eyeD3 / Jinja2 / watchdog、`~=` で固定）。コンテナ内は venv(`/opt/venv`)。

### 動作確認のしかた

専用テストフレームワークは無い。ロジック検証は「ソースをマウントしたコンテナ内でPythonスクリプトを叩く」のが定石（ローカルの eyeD3 CLI は壊れていることがあるため、タグ付け・生成はコンテナ内のeyed3で行う）:

```sh
docker run --rm -e APP_ROOT_URL=http://test.local/ \
  -v /path/to/fixtures:/usr/local/apache2/htdocs/music_files \
  -v "$PWD/app":/usr/src/app \
  podcast-server python3 /usr/src/app/your_test.py
```

**権限・STOPSIGNAL系の不具合（0644配信・docker stopのgraceful停止など）はコンテナ内Pythonテストでは出ず、実コンテナ起動でのみ顕在化する**ので、配信・起動停止に関わる変更は実起動（HTTP応答・`docker stop`のExitCode）で確認すること。

## デプロイ

`.github/workflows/` でDocker Hubへ自動push（マルチアーキ: arm/v7, arm64, amd64、GitHub Actionsキャッシュ有効）:

- **`master` ブランチへのpush** → `:edge` タグを公開（開発版）。
- **`v*` タグのpush** → `:latest` と `:<バージョン>` の両方を公開（例: `v1.2.3` → `:1.2.3`）。

`:latest` はリリースタグ専用。リリース時はバージョンタグ（`v` プレフィックス）を打つ。
