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

## 実装進捗

タスク管理は `spirrow-magickit` の magickit project (`spirrow-conclair`) で追跡。
`design` phase は完了 (T02, T15)、現在は `implementation` phase。
