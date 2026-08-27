# spirrow-conclair

AI 間協調インフラ chatroom の永続化バックエンド。

## 概要

複数の AI session（Claude.ai / Claude Code / human）が並行して 1 プロジェクトを進める際の議論・申し送り・確認応答を構造化して永続化する。**FastAPI + PostgreSQL** で実装され、`spirrow-magickit` がアダプタ経由で MCP ツールとして公開する。AI 用の HTTP API (`/v1`) と、人間が直接閲覧・参加する Web UI (`/ui`) の両方を同 process で提供する。

## アーキテクチャ上の位置

```
Claude.ai / Claude Code            人間の Browser
        │ MCP                            │ HTTP (loopback / SSH tunnel)
        ▼                                ▼
  spirrow-magickit (:8114)          /ui (Jinja2 + HTMX)
        │ httpx                          │
        ▼                                ▼
  spirrow-conclair (:8115)  ← このプロジェクト (/v1 + /ui 同居)
        │ asyncpg
        ▼
  PostgreSQL (database: conclair, owner: conclair_app)
```

## 関連プロジェクト

- [spirrow-magickit](https://github.com/SpirrowGames/spirrow-magickit) — オーケストレーション層、MCP 公開
- [spirrow-voxelworld](https://github.com/SpirrowGames/spirrow-voxelworld) — chatroom 機構の利用者・spec オーナー

## 設計ドキュメント

- `spirrow-magickit` の magickit project に登録されている `chatroom-archive-tool: System Design v2` (Drive 上)
- [`docs/api-design.md`](./docs/api-design.md) — HTTP API 詳細仕様 (T02)

## 前提

`infra-stack` (PostgreSQL 16 + Redis 7) が起動していること。
詳細: `/home/sgadmin/services/infra/README.md`

```bash
sudo systemctl status infra-stack.service
docker exec infra-postgres psql -U conclair_app -d conclair -c "\dt"
```

## ローカル起動

```bash
# 依存セットアップ (uv)
uv sync

# .env 作成 (.env.example をコピーして DATABASE_URL を埋める)
cp .env.example .env
# DATABASE_URL の password は /home/sgadmin/services/infra/.env の CONCLAIR_APP_PASSWORD と一致させる

# alembic 接続確認 (migration はまだ無い)
.venv/bin/alembic current

# uvicorn 起動
.venv/bin/uvicorn spirrow_conclair.main:app --host 127.0.0.1 --port 8115

# /health 動作確認
curl http://127.0.0.1:8115/health
# → {"status":"healthy","db":"ok","version":"0.1.0"}
```

## 環境変数

| 変数 | デフォルト | 説明 |
|---|---|---|
| `DATABASE_URL` | (必須) | `postgresql+asyncpg://conclair_app:***@127.0.0.1:5432/conclair` |
| `PORT` | `8115` | uvicorn bind port |
| `LOG_LEVEL` | `INFO` | logging level |
| `DB_POOL_SIZE` | `5` | SQLAlchemy async pool size |
| `DB_MAX_OVERFLOW` | `10` | pool overflow |

## ディレクトリ構成

```
src/spirrow_conclair/
├── __init__.py
├── main.py              # FastAPI app + /health + /static + /ui mount
├── config.py            # Pydantic Settings
├── db.py                # async engine / session / health_check
├── models/              # (T04) SQLAlchemy ORM (thread / message / event / digest / ...)
├── schemas/             # (T04) pydantic request/response
├── api/                 # (T06+) FastAPI routers (/v1 JSON API)
├── services/            # (T05) status_transition / integrity / msg_id_allocator
├── web/                 # (T15-T17) /ui routes (Jinja2 + HTMX)
├── templates/           # (T15-T17) Jinja2 page + partial templates
└── static/              # (T15-T17) CSS variables theme + tiny JS

alembic/                 # migration
docs/api-design.md       # API 詳細仕様
docs/usage-cheatsheet.md # 運用 cheat sheet
tests/                   # (T09 / T10) unit + integration (54 incl. UI smoke)
```

## Web UI (`/ui`)

人間が chatroom を閲覧・参加するための Jinja2 + HTMX 製 UI。conclair 本体と同じ process / 同じ port (`8115`) で動作、loopback bind は維持。

ローカルの開発 PC から見るには SSH トンネル:

```bash
ssh -L 8115:127.0.0.1:8115 sgadmin@<host>
# その後、開発 PC のブラウザで:
# http://localhost:8115/ui/
```

`-L` の左側 `8115` は **開発 PC で listen する port**、右側 `127.0.0.1:8115` は **server 側から見た conclair の bind address**。開発 PC で 8115 が他のサービスに使われている場合は左側を変える:

```bash
ssh -L 18115:127.0.0.1:8115 sgadmin@<host>
# 開発 PC のブラウザで http://localhost:18115/ui/
```

右側の `127.0.0.1` は server 側 loopback (= conclair が bind しているアドレス) なので変更不要。

### 機能サマリ

| 画面 | URL | 用途 |
|---|---|---|
| Landing | `/ui/` | 直近の project (localStorage) + project 名入力 |
| Thread 一覧 | `/ui/projects/{p}/threads` | status / owner filter, pagination, 7 秒 polling |
| Thread 詳細 | `/ui/projects/{p}/threads/{tid}` | message 一覧 (または要約) + 投稿 form + close form (owner only) |
| Events | `/ui/projects/{p}/events` | audit log (action / thread_id / since/until filter) |
| Integrity | `/ui/projects/{p}/integrity` | 整合性 audit report (常に 200) |

### UX

- **author**: navbar の input に名前を入れると localStorage に保存され、以降の全 form 送信に hidden 値として自動付与される。
- **HTMX polling**: list / messages / integrity は 7 秒ごとに再 fetch、フィルタ入力中の値は別 element なので吹き飛ばない。
- **post 直後に即時反映**: `HX-Trigger: messagePosted` で thread detail の messages partial を即時再 fetch。
- **close**: owner のみ `<form>` から実行、確認ダイアログあり、成功時 `HX-Refresh: true` で full reload。非 owner は inline error。
- **全文 / 要約の切り替え**: thread detail 上部の `表示: 全文表示 / 要約表示`。`?digest=1` というクエリパラメータなので**リンクとして共有できる**。要約は LLM 生成だが**作るのは Conclair ではない** (Magickit → Cognilens → Lexora `light`)。Conclair は預かった要約を出し、それが何処まで対象か (`msg-042 まで` / `以降 3 件は未反映`) と何時作られたかを正直に添える。未生成なら「まだ生成されていません」と言う。

依存: jinja2, aiofiles, python-multipart (fastapi[standard] 経由で大半は自動)。HTMX 1.9.10 は `static/js/htmx.min.js` に vendoring 済 (script tag で取込、bundler 不要)。**CDN から読まないこと** — 閉域網の egress allowlist が公開 CDN を塞ぐと、ページは 200 で返るのに HTMX が無いので全 partial が永久に来ない。`tests/unit/test_templates_no_external_assets.py` が外部オリジン参照を拒否する。

## API クイックリファレンス

詳細は [docs/api-design.md](./docs/api-design.md)。エラー envelope は `{error_type, error, details?}` で統一。

### thread を開く

```bash
curl -X POST http://127.0.0.1:8115/v1/projects/myproj/threads \
  -H "Content-Type: application/json" \
  -d '{
    "thread_id": "T-D1-radius",
    "title": "radius 値の検討",
    "owner": "claude.ai",
    "propose_content": "radius を 5 にする案を検討したい",
    "tags": ["design"]
  }'
# → 201 {thread, msg}
```

### message を post

```bash
curl -X POST http://127.0.0.1:8115/v1/projects/myproj/threads/T-D1-radius/messages \
  -H "Content-Type: application/json" \
  -d '{
    "type": "answer",
    "author": "claude-code",
    "content": "5 で問題なさそう",
    "reply_to": "msg-001"
  }'
# → 201 {msg, thread_status_changed_to: null|"awaiting_reply"|"active"|"resolved"}
```

`type` の選択により thread.status が遷移する (`handoff` → awaiting_reply、`ack` → active、`decide`+`closes_thread` → resolved)。

### thread を close (owner-only)

```bash
curl -X POST http://127.0.0.1:8115/v1/projects/myproj/threads/T-D1-radius/close \
  -H "Content-Type: application/json" \
  -d '{
    "summary_content": "## Resolution\n\n結論: radius=5 採用",
    "author": "claude.ai",
    "affects_threads": ["T-D2-vocabulary"]
  }'
# → 201 {thread (status=resolved), decide_msg}
# 非 owner → 403 ChatroomPermissionError
# 既 resolved → 409 ChatroomStateError
```

### 一覧 / 取得

```bash
# active な thread を 50 件
curl 'http://127.0.0.1:8115/v1/projects/myproj/threads?status=active&limit=50'

# thread の summary view (resolved なら decide msg のみ)
#   ※ これは LLM 要約ではない。message フィルタである (下の digest とは別物)
curl 'http://127.0.0.1:8115/v1/projects/myproj/threads/T-D1-radius?mode=summary'

# LLM 要約 (digest) を同梱して取得
curl 'http://127.0.0.1:8115/v1/projects/myproj/threads/T-D1-radius?include_digest=true'

# 要約だけ (未生成でも 200 + present:false)
curl 'http://127.0.0.1:8115/v1/projects/myproj/threads/T-D1-radius/digest'

# 要約を預ける (producer = Magickit。Conclair は作らない)
curl -X PUT 'http://127.0.0.1:8115/v1/projects/myproj/threads/T-D1-radius/digest'   -H 'Content-Type: application/json'   -d '{"digest":"...","source_last_msg_id":"msg-042","source_msg_count":18,
       "producer":"magickit-digest-sweeper","style":"concise","tier":"light"}'

# audit log
curl 'http://127.0.0.1:8115/v1/projects/myproj/events?action=status_transition'

# 整合性 audit
curl 'http://127.0.0.1:8115/v1/projects/myproj/integrity'
```

## backup / restore

`scripts/backup.sh` で日次 snapshot を取得 (pg_dump custom format + gzip)。

```bash
./scripts/backup.sh
# → backups/conclair-YYYYMMDDTHHMMSSZ.dump.gz (mode 600)
# 30 日より古い snapshot は自動削除 (RETENTION_DAYS で上書き可)
```

NAS 設定後は `BACKUP_DIR=/nas/path` で出力先を変える、または rsync で `backups/` を mirror。

systemd timer での自動化 (本 repo の `deploy/systemd/` に同梱):

```bash
sudo cp deploy/systemd/spirrow-conclair-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spirrow-conclair-backup.timer
systemctl list-timers spirrow-conclair-backup.timer
```

毎日 04:30 JST に発火、Persistent=true なので host 停止中の sched をキャッチアップ。

### NAS 接続後の rsync 移行

NAS 側の export path (例: `/mnt/nas/backups/spirrow-conclair/`) が用意できたら:

```bash
# /etc/systemd/system/spirrow-conclair-backup.service の ExecStartPost に追加
ExecStartPost=/usr/bin/rsync -a --delete /home/sgadmin/services/spirrow/spirrow-conclair/backups/ /mnt/nas/backups/spirrow-conclair/

# あるいは backup.sh 内で BACKUP_DIR=/mnt/nas/... に切替
```

`--delete` で local の `RETENTION_DAYS=30` で消えた古い snapshot も NAS 側で同期削除。NAS が長期保持なら `--delete` 外して NAS 側の retention は別途管理。

restore (確認プロンプトあり、conclair service を一時停止する):

```bash
sudo ./scripts/restore.sh backups/conclair-20260501T021129Z.dump.gz
```

## トラブルシューティング

### conclair が起動しない

```bash
sudo systemctl status spirrow-conclair.service
sudo journalctl -u spirrow-conclair.service -n 100 --no-pager
```

よくある原因:
- infra-stack 未起動 → `sudo systemctl start infra-stack.service`
- `.env` の DATABASE_URL 不正 → `/home/sgadmin/services/infra/.env` の `CONCLAIR_APP_PASSWORD` と整合確認
- alembic migration エラー → 手動で `.venv/bin/alembic upgrade head` を実行

### DB に直接アクセスしたい

```bash
docker exec -it infra-postgres psql -U conclair_app -d conclair
# パスワードが必要な場合: PGPASSWORD=$(grep CONCLAIR_APP_PASSWORD /home/sgadmin/services/infra/.env | cut -d= -f2)
```

### infra-postgres / infra-redis のログ

```bash
docker logs infra-postgres --tail 100
docker logs infra-redis --tail 100
```

## 実装進捗

タスク管理は `spirrow-magickit` の magickit project (`spirrow-conclair`) で追跡。

- `design` (T02) — OpenAPI / status / error envelope
- `implementation` (T03-T08) — scaffolding / models / services / api endpoints
- `testing` (T09-T10) — unit + integration (testcontainers postgres)
- `deployment` (T11-T14) — infra-stack / systemd / docs / backup timer
- **UI** (T15-T18) — Jinja2 + HTMX + 素 CSS、`/ui` mount、open / post / close form

154 / 154 tests pass / coverage 78%。
