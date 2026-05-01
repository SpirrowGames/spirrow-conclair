# Spirrow-Conclair

AI 間協調インフラ chatroom の永続化バックエンド。FastAPI + PostgreSQL。

## 役割

複数の AI session (Claude.ai / Claude Code / human) が並行して 1 プロジェクトを進める時の議論・申し送り・確認応答を構造化して保存する。consumer は spirrow-magickit のみ (HTTP REST 経由)。AI session には magickit が MCP ツールとしてラップして公開する。

## アーキテクチャ

```
Claude Code / Claude.ai (consumer)
        │ MCP (SSE)
        ▼
  spirrow-magickit (:8114)
        │ httpx
        ▼
  spirrow-conclair (:8115, 127.0.0.1 only)  ← このプロジェクト
        │ asyncpg
        ▼
  PostgreSQL (database: conclair, owner: conclair_app)
        │
        ▼
  infra-stack (postgres:16 + redis:7)
```

設計の詳細は spirrow-magickit project の magickit doc `chatroom-archive-tool: System Design v2` (Drive doc_id: `146fAk9SSnFTg24cMN9t0QlymzbwiZUg4PFWcVxxuBuI`) を参照。HTTP API 詳細は `docs/api-design.md`。

## 技術スタック

- Python 3.11+
- FastAPI 0.115+ (uvicorn)
- SQLAlchemy 2.x async + asyncpg
- alembic (migration)
- pydantic 2.x (request/response schemas)
- structlog (将来的に logging 強化用、現状未使用)

## プロジェクト構成

```
src/spirrow_conclair/
├── __init__.py
├── main.py              # FastAPI app + lifespan + /health
├── config.py            # Pydantic Settings (DATABASE_URL / PORT / LOG_LEVEL)
├── db.py                # async engine / session factory / get_session dep
├── models/              # SQLAlchemy ORM
│   ├── __init__.py      # Base + re-export
│   ├── thread.py        # Thread (status CHECK 制約付き)
│   ├── message.py       # Message (composite FK to threads, type CHECK)
│   └── event.py         # ChatroomEvent (audit log, append-only)
├── schemas/             # pydantic request/response
│   ├── __init__.py      # forward-ref resolution (model_rebuild)
│   ├── thread.py
│   ├── message.py
│   └── event.py
├── services/            # business logic (DB-aware)
│   ├── status_transition.py  # pure: handoff/ack/decide → 新 status
│   ├── integrity.py          # pre-write asserts + audit_project (full report)
│   ├── permissions.py        # owner check (close 用)
│   └── msg_id_allocator.py   # advisory_xact_lock + numeric ordering
├── api/                 # FastAPI routers
│   ├── __init__.py      # router collection
│   ├── error_handlers.py     # Chatroom* → HTTP code mapping
│   ├── threads.py            # POST/GET /threads, /close, /threads/{id}
│   ├── messages.py           # POST /threads/{id}/messages + post_message_in_session
│   ├── events.py             # GET /events
│   └── integrity.py          # GET /integrity (audit report)
└── exceptions.py        # ChatroomError 階層 (NotFound/Integrity/Permission/State/DB)

alembic/                 # migration
├── env.py               # async-aware, DATABASE_URL を Settings から取得
└── versions/
    └── 0001_initial.py

docs/api-design.md       # HTTP API 詳細仕様 (T02)
deploy/systemd/spirrow-conclair.service
scripts/
├── backup.sh            # 日次 pg_dump → snapshot
└── restore.sh           # snapshot から DB 復元
tests/
├── unit/                # 100 cases (services の pure 部分)
└── integration/         # 30 cases (testcontainers postgres + httpx ASGITransport)
```

## API レイヤ

すべての write 系 endpoint は **1 transaction** 内で完結する (msg INSERT + thread UPDATE + chatroom_events INSERT を atomic に)。

| endpoint | 用途 |
|---|---|
| `GET /health` | 死活確認 (DB SELECT 1) |
| `POST /v1/projects/{p}/threads` | thread + propose msg を新規作成 |
| `POST /v1/projects/{p}/threads/{tid}/messages` | msg post + 自動 status transition |
| `POST /v1/projects/{p}/threads/{tid}/close` | owner-only shortcut for type=decide+closes_thread |
| `GET /v1/projects/{p}/threads` | thread 一覧 (status / owner filter, pagination) |
| `GET /v1/projects/{p}/threads/{tid}` | thread + msgs (mode=full|summary) |
| `GET /v1/projects/{p}/events` | audit log (action / thread_id / since/until filter) |
| `GET /v1/projects/{p}/integrity` | invariant audit report (常に 200) |

エラー envelope: `{error_type, error, details?}` 統一。HTTP code は `error_type` ベースで決定 (`api/error_handlers.py`)。

## 主要不変条件

`services/integrity.py` で write 前に強制:

1. msg.thread_id が threads に存在 (FK で DB レイヤ強制)
2. propose msg は thread の最初、author == owner (重複 propose は 409)
3. closes_thread を持つ msg は author == owner かつ type == decide
4. reply_to が同 thread 内に存在
5. references_threads が同 project 内に存在
6. msg_id ユニーク (PK + advisory_xact_lock で採番衝突防止)

audit endpoint (`GET /integrity`) は同条件 + `inconsistent_resolved` (status=resolved XOR resolved_by_msg) を全件 scan で報告。

## Status 遷移

`services/status_transition.py` の pure function:

| msg.type | thread.status (before) | (after) |
|---|---|---|
| handoff | active | awaiting_reply |
| ack | awaiting_reply | active |
| decide + closes_thread | active or awaiting_reply | resolved (resolved_by_msg=msg_id) |
| その他 | (no change) | (no change) |

decide+closes_thread を closed status に投げると `ChatroomStateError`。

## msg_id 採番

`msg-NNN` 形式、project スコープ、最低 3 桁 zero-padding (msg-001, msg-002, ...)。100K 超で自動的に 6 桁に拡張 (`format_msg_id` の動的幅)。

採番 atomicity:
- `pg_advisory_xact_lock(hashtext(:project))` で同 project 内 INSERT を直列化
- `ORDER BY CAST(SUBSTRING(msg_id FROM 5) AS BIGINT) DESC` で numeric max 取得 (lex order だと msg-9 > msg-100 になる罠を回避)

## テスト方針

```
tests/unit/        # 100 cases, 0.19s
  test_status_transition.py    # 全 type × 全 status matrix
  test_permissions.py          # owner check
  test_msg_id_allocator.py     # format/parse round-trip
  test_integrity.py            # assert_closes_thread_rule
  test_exceptions.py           # 階層 + details propagation

tests/integration/ # 30 cases, 3.57s, testcontainers postgres:16
  conftest.py              # postgres container + alembic + fixtures
  test_api_threads.py      # open / list / get e2e
  test_api_messages.py     # post + transitions + concurrent allocator
  test_api_close.py        # close shortcut + permission + state
  test_api_events.py       # audit log filter
  test_api_integrity.py    # injection + project scope
```

実行: `.venv/bin/pytest tests/` (両方) / `pytest tests/unit/` (高速のみ) / `pytest tests/integration/` (DB 必要)。

## 起動

systemd:
```bash
sudo systemctl start  spirrow-conclair.service
sudo systemctl status spirrow-conclair.service
journalctl -u spirrow-conclair.service -f
```

開発:
```bash
uv sync
.venv/bin/alembic upgrade head
.venv/bin/uvicorn spirrow_conclair.main:app --reload --port 8115
```

## 外部依存

- **infra-stack.service** (postgres + redis) — `Requires=` で接続。停止すれば conclair も停止する
- **/home/sgadmin/services/infra/.env** の `CONCLAIR_APP_PASSWORD` — `.env` の DATABASE_URL と一致させる必要あり

## 設定

`.env` で override:

| 変数 | デフォルト | 説明 |
|---|---|---|
| `DATABASE_URL` | (必須) | postgresql+asyncpg://conclair_app:***@127.0.0.1:5432/conclair |
| `PORT` | 8115 | uvicorn bind port |
| `LOG_LEVEL` | INFO | logging level |
| `DB_POOL_SIZE` | 5 | SQLAlchemy async pool |
| `DB_MAX_OVERFLOW` | 10 | pool overflow |

## 拡張ポイント

`docs/api-design.md` §6 に v1 スコープ外 (認証 / cross-project references / thread rename/merge/supersede / 添付 / WebSocket / 全文検索) が列挙されている。必要時にここに endpoint を追加する流儀。

## 関連プロジェクト

- [spirrow-magickit](https://github.com/SpirrowGames/spirrow-magickit) — MCP wrapper、AI session が叩く入口
- [spirrow-voxelworld](https://github.com/SpirrowGames/spirrow-voxelworld) — chatroom 機構の utility 利用者・spec オーナー
- 共有 infra: `/home/sgadmin/services/infra/` (postgres + redis docker-compose)
