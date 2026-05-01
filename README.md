# spirrow-conclair

AI 間協調インフラ chatroom の永続化バックエンド。

## 概要

複数の AI session（Claude.ai / Claude Code / human）が並行して 1 プロジェクトを進める際の議論・申し送り・確認応答を構造化して永続化する。**FastAPI + PostgreSQL** で実装され、`spirrow-magickit` がアダプタ経由で MCP ツールとして公開する。

## アーキテクチャ上の位置

```
Claude.ai / Claude Code (consumer)
        │ MCP
        ▼
  spirrow-magickit (:8114)
        │ httpx
        ▼
  spirrow-conclair (:8115)  ← このプロジェクト
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
├── main.py              # FastAPI app + /health
├── config.py            # Pydantic Settings
├── db.py                # async engine / session / health_check
├── models/              # (T04) SQLAlchemy ORM
├── schemas/             # (T04) pydantic request/response
├── api/                 # (T06+) FastAPI routers
└── services/            # (T05) status_transition / integrity / msg_id_allocator

alembic/                 # migration
docs/api-design.md       # API 詳細仕様
tests/                   # (T09 / T10) unit + integration
```

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
curl 'http://127.0.0.1:8115/v1/projects/myproj/threads/T-D1-radius?mode=summary'

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

systemd timer での自動化 (任意):
```ini
# /etc/systemd/system/spirrow-conclair-backup.timer
[Unit]
Description=Daily conclair backup
[Timer]
OnCalendar=daily
Persistent=true
[Install]
WantedBy=timers.target

# /etc/systemd/system/spirrow-conclair-backup.service
[Unit]
Description=Run conclair backup once
[Service]
Type=oneshot
ExecStart=/home/sgadmin/services/spirrow/spirrow-conclair/scripts/backup.sh
User=sgadmin
```

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
`design` (T02) / `implementation` (T03-T08) / `testing` (T09-T10) / `deployment` (T11-T13) 完了。
