---
id: spirrow-conclair:system-design-v2
title: 'chatroom-archive-tool: System Design (v2)'
product: spirrow-conclair
type: design
status: active
version: 2.0
created: 2026-05-01
last_verified: 2026-09-10
supersedes: []
related: [spirrow-conclair:api-design, spirrow-conclair:chatroom-operating-rules, platform:infra-registry]
keywords: [conclair, chatroom, PostgreSQL, FastAPI, alembic, magickit, ChatroomAdapter, advisory lock]
legacy_drive_id: [146fAk9SSnFTg24cMN9t0QlymzbwiZUg4PFWcVxxuBuI]
---

# chatroom-archive-tool: System Design (v2)

**Status**: Design v2 (T15)
**Date**: 2026-05-01 (DB naming corrected)
**Author**: Claude Code ({{HOST_SERVICES}} session)
**Supersedes**: v1 (SQLite + magickit 内蔵案、本 doc にて全面改訂)
**Related projects**:
- `spirrow-magickit` — adapter / MCP wrapper / 本 doc 管理元
- `spirrow-conclair` — chatroom 永続化バックエンド本体（新規 repo: https://github.com/SpirrowGames/spirrow-conclair ）
- `spirrow-voxelworld` — chatroom spec オーナー、README v0.2 改訂が別途必要

---

## 0. 名称について

タスク群名 "chatroom-archive-tool" は task 連番との整合のため維持。実体は **spirrow-conclair** という独立サービス（FastAPI バックエンド）。本 doc 内では region 名として "chatroom" を使う。

---

## 1. v1 からの主要変更点

| 項目 | v1 | v2 |
|---|---|---|
| storage | SQLite (magickit 内蔵 db ファイル) | **PostgreSQL** (Thirdy と同居 cluster, database `conclair`) |
| 配置 | magickit プロセス内モジュール | **独立 FastAPI service `spirrow-conclair` (port 8115)** |
| migration | 手書き SQL | **alembic** |
| ORM | 生 sqlite3 | **SQLAlchemy 2.x async + asyncpg** |
| infra | (なし) | **infra docker-compose (postgres + redis)** を Thirdy と分離 |
| magickit 側 | adapter + MCP + ロジック全部 | **薄い ChatroomAdapter (httpx) + MCP ツール 7 個** のみ |
| split-readiness | 単一 file | DB 単位 isolation で将来 cluster 分離容易（接続 URL 変更のみ） |

理由:
- {{HOST_SERVICES}} hub 化で外部依存（Drive 等）を捨てる方針
- Thirdy が PostgreSQL 16 + Redis を前提としており、infra として共有するのが自然
- chatroom が成長したり別 consumer (Web UI 等) が現れた時に独立 service の方が拡張しやすい
- magickit 本体の責務肥大化を避ける（adapter pattern 維持）

サービス単位 isolation 原則: **各 service は専用 role + 専用 DB を持つ**。conclair は `conclair_app` role が `conclair` DB を所有する。将来 magickit が独自 persistence を持つ時は別 DB (`magickit`) を切る。cross-DB 結合は作らない。

## 2. アーキテクチャ全体

```
Claude Code / Claude.ai (consumer)
        │ MCP (SSE)
        ▼
  spirrow-magickit (:8114)
   ├ ChatroomAdapter (httpx)
   └ MCP tools: chatroom_open_thread / post_message / close_thread /
                  list_threads / get_thread / list_events / check_integrity
        │ HTTP (REST)
        ▼
  spirrow-conclair (:8115)
   ├ FastAPI app
   ├ services: status_transition / integrity / msg_id allocator
   └ SQLAlchemy async + asyncpg
        │
        ▼
  PostgreSQL 16 (database `conclair`, owner `conclair_app`)
   └ tables: threads / messages / chatroom_events / alembic_version
```

infra 層:
```
infra docker-compose (新設):
  - postgres:16        (port 5432, volume: pgdata)
  - redis:7-alpine     (port 6379, volume: redisdata)

Thirdy compose (既存改修):
  - api / worker / web のみ。postgres/redis は external 参照
```

---

## 3. infra layout

### 3.1 ディレクトリ

```
{{PATH_SERVICES_ROOT}}/infra/
├── docker-compose.yml          # postgres + redis only
├── .env                        # POSTGRES_PASSWORD 等
└── README.md

{{PATH_SERVICES}}/spirrow-conclair/
└── (新 repo)

{{PATH_SERVICES_ROOT}}/thirdy/
└── docker-compose.yml          # postgres/redis 参照を external に書き換え
```

### 3.2 infra docker-compose

```yaml
# {{PATH_SERVICES_ROOT}}/infra/docker-compose.yml
services:
  postgres:
    image: postgres:16
    container_name: infra-postgres
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: ${POSTGRES_SUPER_PASSWORD}
      THIRDY_APP_PASSWORD: ${THIRDY_APP_PASSWORD}
      CONCLAIR_APP_PASSWORD: ${CONCLAIR_APP_PASSWORD}
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./initdb:/docker-entrypoint-initdb.d:ro
    deploy:
      resources:
        limits:
          memory: 2G

  redis:
    image: redis:7-alpine
    container_name: infra-redis
    ports:
      - "127.0.0.1:6379:6379"
    volumes:
      - redisdata:/data
    deploy:
      resources:
        limits:
          memory: 256M

volumes:
  pgdata:
  redisdata:
```

`initdb/` 配下に database / role 初期化 .sh:

```bash
# {{PATH_SERVICES_ROOT}}/infra/initdb/00_init_databases.sh
psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<EOSQL
DO \$\$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='thirdy_app') THEN
        CREATE ROLE thirdy_app LOGIN PASSWORD '${THIRDY_APP_PASSWORD}';
    END IF;
END \$\$;
DO \$\$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='conclair_app') THEN
        CREATE ROLE conclair_app LOGIN PASSWORD '${CONCLAIR_APP_PASSWORD}';
    END IF;
END \$\$;

SELECT 'CREATE DATABASE thirdy OWNER thirdy_app'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='thirdy')\gexec
SELECT 'CREATE DATABASE conclair OWNER conclair_app'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='conclair')\gexec
EOSQL
```

secret は env 経由で注入 (psql `:'var'` 構文は DO ブロックで効かないため `.sh` + here-doc が必要)。

### 3.3 systemd 統合

`/etc/systemd/system/infra-stack.service` (system-level):
```ini
[Unit]
Description=Infra stack (postgres + redis) for {{HOST_SERVICES}}
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory={{PATH_SERVICES_ROOT}}/infra
EnvironmentFile={{PATH_SERVICES_ROOT}}/infra/.env
ExecStart=/usr/bin/docker compose up -d --remove-orphans
ExecStop=/usr/bin/docker compose stop
TimeoutStartSec=180

[Install]
WantedBy=multi-user.target
```

これで infra layer がブート時に上がる。Thirdy / spirrow-conclair / 将来追加されるサービスは全部この上で動く。

### 3.4 Thirdy 側の改修（別タスク）

`{{PATH_SERVICES_ROOT}}/thirdy/docker-compose.yml` を改修:
- `postgres` / `redis` service を削除
- `api` / `worker` の DATABASE_URL / REDIS_URL を `infra-postgres:5432` / `infra-redis:6379` (network 共有で hostname 解決)
- networks に `infra` (external=true) を追加し、api/worker が join

これは spirrow-conclair の scope 外、Thirdy の独立タスクとする (T11 で完了済)。

---

## 4. spirrow-conclair プロジェクト構造

```
spirrow-conclair/
├── pyproject.toml                 # FastAPI 0.115+, SQLAlchemy 2.x, asyncpg, alembic, pydantic 2
├── uv.lock                        # uv lockfile (既存 spirrow stack に揃える)
├── alembic.ini
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 0001_initial.py        # threads / messages / chatroom_events
├── src/spirrow_conclair/
│   ├── __init__.py
│   ├── main.py                    # FastAPI app + lifespan
│   ├── config.py                  # Settings (DB URL, port, log level)
│   ├── db.py                      # async engine / session_factory / get_session dep
│   ├── models/                    # SQLAlchemy ORM
│   │   ├── __init__.py
│   │   ├── thread.py
│   │   ├── message.py
│   │   └── event.py
│   ├── schemas/                   # pydantic request / response
│   │   ├── thread.py
│   │   ├── message.py
│   │   └── event.py
│   ├── api/                       # FastAPI router
│   │   ├── __init__.py
│   │   ├── threads.py
│   │   ├── messages.py
│   │   ├── events.py
│   │   └── integrity.py
│   ├── services/                  # business logic
│   │   ├── __init__.py
│   │   ├── status_transition.py
│   │   ├── integrity.py
│   │   ├── permissions.py
│   │   └── msg_id_allocator.py
│   └── exceptions.py
├── docs/
│   └── api-design.md              # T02 で確定 (HTTP API 詳細仕様)
├── tests/
│   ├── conftest.py                # pytest-asyncio + tmp pg (testcontainers)
│   ├── unit/
│   └── integration/
├── deploy/
│   └── systemd/
│       └── spirrow-conclair.service
└── README.md
```

---

## 5. PostgreSQL schema (alembic 0001_initial)

```python
# alembic/versions/0001_initial.py
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

def upgrade():
    op.create_table(
        "threads",
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("thread_id", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_by_msg", sa.Text, nullable=False),
        sa.Column("resolved_by_msg", sa.Text, nullable=True),
        sa.Column("affects_threads", JSONB, nullable=False, server_default="[]"),
        sa.Column("tags", JSONB, nullable=False, server_default="[]"),
        sa.PrimaryKeyConstraint("project", "thread_id"),
        sa.CheckConstraint(
            "status IN ('active','awaiting_reply','resolved','superseded','parked')",
            name="threads_status_check",
        ),
    )
    op.create_index("idx_threads_status", "threads", ["project", "status"])
    op.create_index("idx_threads_owner", "threads", ["project", "owner"])

    op.create_table(
        "messages",
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("msg_id", sa.Text, nullable=False),
        sa.Column("thread_id", sa.Text, nullable=False),
        sa.Column("author", sa.Text, nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("commit_ref", sa.Text, nullable=True),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("reply_to", sa.Text, nullable=True),
        sa.Column("references_threads", JSONB, nullable=False, server_default="[]"),
        sa.Column("related_tasks", JSONB, nullable=False, server_default="[]"),
        sa.Column("closes_thread", sa.Text, nullable=True),
        sa.Column("tags", JSONB, nullable=False, server_default="[]"),
        sa.PrimaryKeyConstraint("project", "msg_id"),
        sa.ForeignKeyConstraint(
            ["project", "thread_id"],
            ["threads.project", "threads.thread_id"],
            name="messages_thread_fkey",
        ),
        sa.CheckConstraint(
            "type IN ('propose','question','answer','decide','report','handoff','ack')",
            name="messages_type_check",
        ),
    )
    op.create_index("idx_messages_thread", "messages", ["project", "thread_id"])
    op.create_index("idx_messages_type", "messages", ["project", "type"])

    op.create_table(
        "chatroom_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("thread_id", sa.Text, nullable=True),
        sa.Column("msg_id", sa.Text, nullable=True),
        sa.Column("details", JSONB, nullable=False, server_default="{}"),
    )
    op.create_index("idx_events_project_ts", "chatroom_events", ["project", "timestamp"])
    op.create_index("idx_events_thread", "chatroom_events", ["project", "thread_id"])

def downgrade():
    op.drop_table("chatroom_events")
    op.drop_table("messages")
    op.drop_table("threads")
```

JSONB 採用理由: PostgreSQL のネイティブ機能、`@>` / `?|` 等で配列クエリ可能。SQLite の JSON 列より型サポートが厚い。

---

## 6. SQLAlchemy ORM models（要点）

```python
# models/thread.py
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import Text, TIMESTAMP
from datetime import datetime

class Base(DeclarativeBase): pass

class Thread(Base):
    __tablename__ = "threads"
    project: Mapped[str] = mapped_column(Text, primary_key=True)
    thread_id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    created_by_msg: Mapped[str] = mapped_column(Text)
    resolved_by_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    affects_threads: Mapped[list[str]] = mapped_column(JSONB, default=list)
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)

# models/message.py / models/event.py 同様
```

---

## 7. FastAPI HTTP API

各 endpoint は magickit MCP ツールの 1:1 対応。詳細は `docs/api-design.md` (T02 で確定) を参照。要点のみ:

| MCP tool | HTTP endpoint |
|---|---|
| `chatroom_open_thread` | `POST /v1/projects/{project}/threads` |
| `chatroom_post_message` | `POST /v1/projects/{project}/threads/{thread_id}/messages` |
| `chatroom_close_thread` | `POST /v1/projects/{project}/threads/{thread_id}/close` |
| `chatroom_list_threads` | `GET  /v1/projects/{project}/threads?status=&owner=&limit=&offset=` |
| `chatroom_get_thread` | `GET  /v1/projects/{project}/threads/{thread_id}?mode=full\|summary` |
| `chatroom_list_events` | `GET  /v1/projects/{project}/events?thread_id=&action=&since=&limit=&offset=` |
| `chatroom_check_integrity` | `GET  /v1/projects/{project}/integrity` |

リクエスト / レスポンスは pydantic schema で固定。バリデーションエラーは 422、整合性エラーは 409、権限エラーは 403、not found は 404、それ以外は 500。エラー形式: `{error_type, error, details?}` で統一。

### 7.1 認証

認証の姿勢とその判断の根拠は [[platform:infra-registry]] §5 にある（規約 §3.1-4 により本書には書かない）。bind と port の実体は §13.1 の systemd unit がそのまま示している。

---

## 8. status 遷移ロジック

`services/status_transition.py` に pure function:

```python
def compute_transition(
    thread: Thread, new_msg: Message,
) -> tuple[str | None, dict]:
    if new_msg.type == "handoff" and thread.status == "active":
        return ("awaiting_reply", {})
    if new_msg.type == "ack" and thread.status == "awaiting_reply":
        return ("active", {})
    if (new_msg.type == "decide"
        and new_msg.closes_thread == thread.thread_id
        and thread.status in ("active", "awaiting_reply")):
        return ("resolved", {"resolved_by_msg": new_msg.msg_id})
    return (None, {})
```

`superseded` / `parked` への遷移は v1 API では提供しない（運用上稀、必要時に PATCH /threads/{id}/status エンドポイント追加）。

---

## 9. 整合性 invariants（write 前検証）

1. msg.thread_id が threads に存在
2. propose msg は thread の最初の msg、author == thread.owner
3. closes_thread を持つ msg は author == thread.owner かつ type == "decide"
4. reply_to 参照先が同 thread 内に存在
5. references_threads の各 thread_id が threads に存在（同 project 内のみ）
6. msg_id ユニーク（PK 強制 + advisory lock で採番衝突防止）

違反は `ChatroomIntegrityError`。

### 9.1 msg_id 採番

「msg-NNN」形式を維持。INSERT 時の手順:

```python
async with session.begin():  # transaction
    await session.execute("SELECT pg_advisory_xact_lock(hashtext(:project))",
                          {"project": project})
    row = await session.execute(
        "SELECT msg_id FROM messages WHERE project=:p ORDER BY msg_id DESC LIMIT 1",
        {"p": project},
    )
    last = row.scalar_one_or_none()
    next_n = (int(last.split("-")[1]) + 1) if last else 1
    new_id = f"msg-{next_n:03d}"
    # INSERT msg with new_id
```

`pg_advisory_xact_lock` で同 project 内の同時 INSERT を直列化。100K msg を超えたら format を `msg-NNNNN` に拡張（運用 rule、code は zero-padding を可変に）。

---

## 10. エラー階層

```python
class ChatroomError(Exception): ...
class ChatroomNotFoundError(ChatroomError): ...      # 404
class ChatroomIntegrityError(ChatroomError): ...     # 409
class ChatroomPermissionError(ChatroomError): ...    # 403
class ChatroomStateError(ChatroomError): ...         # 409 (invalid transition)
class ChatroomDBError(ChatroomError): ...            # 500
```

FastAPI exception handler でステータスコードに変換。

---

## 11. magickit 側の薄い wrapper

### 11.1 ChatroomAdapter

```python
# magickit/adapters/chatroom.py
class ChatroomAdapter:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def open_thread(self, *, project: str, **kw) -> dict: ...
    async def post_message(self, *, project: str, thread_id: str, **kw) -> dict: ...
    async def close_thread(self, *, project: str, thread_id: str, **kw) -> dict: ...
    async def list_threads(self, *, project: str, **kw) -> dict: ...
    async def get_thread(self, *, project: str, thread_id: str, mode: str = "full") -> dict: ...
    async def list_events(self, *, project: str, **kw) -> dict: ...
    async def check_integrity(self, *, project: str) -> dict: ...

    async def health_check(self) -> bool:
        r = await self._client.get("/health")
        return r.status_code == 200
```

### 11.2 MCP ツール登録

`magickit/mcp/tools/chatroom.py` に `register_tools(mcp, settings)` を追加し、`mcp_server.py` で他のツールと並列に登録。各ツールは `ChatroomAdapter` を呼ぶ薄い委譲。

### 11.3 設定

`magickit/config.py` に追加:

```python
class Settings(...):
    conclair_url: str = "http://localhost:8115"
    conclair_timeout: float = 30.0
```

env override: `MAGICKIT_CONCLAIR_URL` / `MAGICKIT_CONCLAIR_TIMEOUT`

---

## 12. テスト戦略

### 12.1 spirrow-conclair 単体

- **unit**:
  - `services/status_transition.py` matrix（全 type × 全 status）
  - `services/integrity.py` 各 invariant 違反パターン
  - `services/msg_id_allocator.py` 連番 / advisory lock の挙動（concurrent 並行 insert）
- **integration**:
  - 実 PostgreSQL（testcontainers-python or docker compose の test db）に対して全 endpoint を叩く
  - alembic migration の up/down 往復確認
  - JSONB 配列フィルタ（tags / affects_threads）

### 12.2 magickit 側

- **unit**: ChatroomAdapter の各メソッドを `respx` (httpx mock) でアサート
- **MCP ツール**: tool 経由で adapter が正しく呼ばれることを mock で確認
- **integration**: spirrow-conclair を test container として立てて magickit MCP 経由で e2e

---

## 13. デプロイ

### 13.1 systemd unit (spirrow-conclair)

```ini
# /etc/systemd/system/spirrow-conclair.service (system-level)
[Unit]
Description=Spirrow Conclair - chatroom persistence backend
After=infra-stack.service network-online.target
Requires=infra-stack.service

[Service]
Type=simple
User={{USER_SERVICES}}
WorkingDirectory={{PATH_SERVICES}}/spirrow-conclair
EnvironmentFile={{PATH_SERVICES}}/spirrow-conclair/.env
ExecStartPre={{PATH_SERVICES}}/spirrow-conclair/.venv/bin/alembic upgrade head
ExecStart={{PATH_SERVICES}}/spirrow-conclair/.venv/bin/uvicorn \
    spirrow_conclair.main:app --host 127.0.0.1 --port 8115
MemoryMax=2G
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`.env` の中身 (例):
```
DATABASE_URL=postgresql+asyncpg://conclair_app:${CONCLAIR_APP_PASSWORD}@127.0.0.1:5432/conclair
PORT=8115
LOG_LEVEL=INFO
```

migration は起動時に `alembic upgrade head` で自動 apply。

### 13.2 backup（NAS 設定後）

infra-postgres コンテナの `pgdata` を対象に `pg_dump -U postgres -d conclair` の日次 dump → NAS rsync。
Thirdy DB と独立した backup chain で運用。

---

## 14. 仕様 spec への影響

spirrow-voxelworld の `Docs/percell-lod/chatroom/README.md` v0.1 → **v0.2 改訂が必要**:
- §2 ファイル構造: file 中心 → API 中心 (magickit MCP ツール経由)
- §3-§8: scheme は維持（PostgreSQL 列に対応）
- §9 Session 開始時のルール: file open → MCP ツール呼び出し
- §11 作業境界: msg post も magickit MCP 経由に
- §11.1 magickit 連携: 「実装済 (spirrow-conclair + magickit ChatroomAdapter)」に書き換え
- §12 msg_id 採番: git merge → DB advisory lock
- §16 既知の限界: ファイル grep → SQL クエリで対応可

改訂は spirrow-voxelworld 側で `T-meta-chatroom-design-v2` thread を立てて議論（README §15 ルール）。spirrow-magickit / spirrow-conclair の実装は spec 改訂を待たずに先行可能。

---

## 15. v1 → v2 で消える概念 / 出てくる概念

### 消える
- markdown parser（threads.md / messages.md / archive/T-XXX.md）
- atomic file rename / file lock
- file 直接 git commit を介した audit trail

### 新規
- alembic migration 管理
- PostgreSQL advisory lock for msg_id
- chatroom_events table が audit trail の代替
- HTTP API レイヤ（FastAPI）
- spirrow-conclair systemd unit + infra docker-compose 依存

---

## 16. タスク再編 plan

### spirrow-magickit project（既存）
旧 T16-T21 を delete、以下を新規登録 (T23-T26):
- T23 (impl): ChatroomAdapter (httpx)
- T24 (impl): MCP ツール 7 個 (`chatroom_*`) 登録 + config
- T25 (test): adapter / MCP unit test (respx + mock)
- T26 (deploy): magickit CLAUDE.md / mcp_server.py 配線、conclair 起動依存設定

### spirrow-conclair project（新規 magickit project、template=mcp-server）
- design phase:
  - T02 (詳細設計レビュー / OpenAPI スキーマ確定) — 完了 (docs/api-design.md)
- implementation phase:
  - T03 (scaffolding): pyproject / alembic / .venv / 雛形 main.py
  - T04 (models + alembic 0001_initial)
  - T05 (services: status_transition / integrity / msg_id_allocator)
  - T06 (api: threads endpoint)
  - T07 (api: messages endpoint + status transition 連携)
  - T08 (api: close + events + integrity endpoint)
- testing phase:
  - T09 (unit test 一式)
  - T10 (integration test on real Postgres / testcontainers)
- deployment phase:
  - T11 (infra-stack.service / Thirdy compose 改修) — 完了
  - T12 (conclair systemd unit + .env / Memory limit)
  - T13 (README + 運用 cheat sheet)

### spirrow-voxelworld project
- T-meta-chatroom-design-v2 thread を chatroom (まだ markdown 運用) で立てる
- README v0.2 への書き換え PR は thread resolve 後に実施

依存関係:
- conclair T03 〜 T08 完了 → magickit T23 〜 T25 着手可能
- conclair T12 完了 → magickit T26 着手可能
- 全部完了 → spec 改訂 PR

---

## 17. 既知の限界 / 将来拡張

### v1 で意図的に省略
- `superseded` / `parked` への遷移 API
- thread 横断 graph 可視化
- standalone announcement msg
- multi-project cross-reference
- 添付ファイル (msg に diff/patch attach)
- 認証層 — 姿勢と根拠は [[platform:infra-registry]] §5（規約 §3.1-4）

### 将来検討
- summary post format の自動検証 (Lexora 利用)
- long-thread 自動要約
- magickit task module 直結 (`related_tasks` から task 詳細 resolve)
- Web UI dashboard
- multi-tenant 化（複数組織のサポート）
- PostgreSQL 全文検索（msg.content への GIN インデックス）

---

## 18. 改訂履歴

- 2026-05-01 初版 v2 作成 (SQLite v1 → PostgreSQL + spirrow-conclair 分離)
- 2026-05-01 DB / role 命名修正: `magickit` / `magickit_app` → `conclair` / `conclair_app` (サービス単位 isolation の原則を明示化)

---

**End of System Design v2**

---

## 移行時の注記（2026-09-10）

Drive 原本（`146fAk9SSnFTg24cMN9t0QlymzbwiZUg4PFWcVxxuBuI`）の移行。

**この repo は、git に一度も存在したことのない文書を 9 箇所から引用していた。**
[[platform:reconciliation-small-projects]] §4.1 は本書の移行先を「Magickit ではない可能性が高い、
本文を読んでから決める」と保留していたが、決め手は Drive 側ではなく conclair 側にあった:

| 引用元 | 内容 |
|---|---|
| `alembic/versions/0001_initial.py:7` | 「Faithfully implements the schema in chatroom-archive-tool: System Design v2 §5」 |
| `src/spirrow_conclair/services/integrity.py:1` | 「Integrity invariants (System Design v2 §9)」 |
| `src/spirrow_conclair/services/msg_id_allocator.py:10` | 「Per System Design v2 §9.1」 |
| `src/spirrow_conclair/services/status_transition.py:3` | 「Per System Design v2 §8 / api-design.md §3.2」 |
| `docs/api-design.md:5` | `**Source**: T15 v2 (System Design, doc_id: ...)` |
| `docs/usage-cheatsheet.md` | 「設計の "なぜ": magickit project の Drive doc」 |
| `CLAUDE.md:31` / `README.md:31` | 同じく Drive doc への参照 |

∴ **移行先は `spirrow-magickit` ではなく `spirrow-conclair`。** 本書を引用しているコードがここにある。

### 逐語からの逸脱

| 箇所 | 対応 | 根拠 |
|---|---|---|
| §Author / §1 理由 / §3.3 / §7.1 の実ホスト名 4 件 | `{{HOST_SERVICES}}` に置換 | 規約 §3.1-2 |
| §3.1 / §3.2 / §3.4 / §13.1 のサーバーパス | `{{PATH_SERVICES_ROOT}}` / `{{PATH_SERVICES}}` に置換 | 同上 |
| §13.1 systemd unit の `User=` | `{{USER_SERVICES}}` に置換 | 同上 |
| **§7.1 認証 の本文** | [[platform:infra-registry]] §5 への参照に置換 | **§3.1-4（置換ではなく移動）** |
| **§17「認証層（loopback bind のみ）」** | 同上 | 同上 |

ポート（5432 / 6379 / 8114 / 8115）は実値のまま（§3.1-3）。台帳 §3 に 4 件とも登録済み。
`127.0.0.1` は loopback の普遍値でホストを指さないため置換していない。

**移した内容の要点**: Conclair には認証層が無く、`127.0.0.1` bind だけが信頼境界であること。
外部公開するなら API key middleware の追加が前提条件であること。台帳側にはこの判断の根拠ごと
記録してある（`spirrow-docs#23`）。

### 現物と食い違っている箇所（本書を現状として読まないこと）

**§7 の 7 endpoint 表は現在の API surface ではない。** repo の `src/spirrow_conclair/api/` は
本書が想定していなかった router を 4 本持っている:

| router | prefix |
|---|---|
| `control.py` | `/v1/projects/{project}/control` |
| `digest.py` | `/v1/projects/{project}/threads/{thread_id}/digest` |
| `read_cursor.py` | `/v1/projects/{project}/threads/{thread_id}/read` + `/unread` |
| `projects.py` | `/v1/projects` |

MCP ツールも 7 個ではなく **9 個**（`chatroom_mark_read` / `chatroom_my_unread` が増えた）。
alembic も `0001_initial` だけでなく `0008_thread_digests` まで進んでおり、`messages` には
本書に無い `role` / `next_participant` 列がある。

**§16 タスク再編 plan は 2026-05-01 時点の計画で、全て消化済み。** 現状として読まない。

**§8 の `compute_transition` は本書のコードとは分岐が 1 つ違う。** 現物は
`decide` + `closes_thread` 一致が **closed thread に来た場合に `ChatroomStateError` を上げる**
（本書のコードは黙って `(None, {})` を返す）。`services/status_transition.py` に、なぜ
raise しないと silent half-close になるかの実測込みの説明がある。

### 変わっていない箇所（本書がまだ正本）

§1（PostgreSQL / 独立 service / alembic / SQLAlchemy async の採用理由とサービス単位 isolation 原則）、
§3（infra-stack の構成）、§5（3 テーブルのスキーマ）、§9（6 つの invariant）、§9.1（advisory lock による
msg_id 採番）、§13（systemd 配下での起動と backup chain）は現物と一致している。
**repo のコードを読んでも出てこないのは「なぜ SQLite をやめたか」「なぜ DB を service ごとに分けるか」** で、
それは §1 にしかない。
