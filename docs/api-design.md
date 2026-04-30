# spirrow-conclair API Design (v1)

**Status**: Design (T02)
**Date**: 2026-05-01
**Source**: T15 v2 (System Design, doc_id: `146fAk9SSnFTg24cMN9t0QlymzbwiZUg4PFWcVxxuBuI`)

本書は spirrow-conclair の HTTP API 詳細仕様。実装タスク (T03 以降) はこの仕様書を見ながら淡々と実装できる粒度を目指す。

---

## 1. 設計判断 (cross-cutting concerns)

### 1.1 認証・認可 (v1)

- bind: `127.0.0.1:8115` のみ。外部から到達不能。
- 認証: なし。loopback のみが信頼境界。
- 認可: なし。caller が claim する `author` 文字列に基づく honor-system check のみ (close_thread の owner check 等)。

将来 (post v1): 必要時に API key middleware を `/v1/` 配下に追加。

### 1.2 timestamp 取扱い

- すべての timestamp は **ISO 8601 UTC**、`Z` suffix で固定。例: `2026-05-01T19:51:59Z`。
- DB column は `TIMESTAMP WITH TIME ZONE` (PostgreSQL `timestamptz`)。
- server 側で生成する timestamp はサーバ時刻 (UTC normalize)。
- client が timestamp を送信した場合は parse 時に UTC 変換してから保存。
- レスポンスは常に UTC `Z` suffix。

理由: サーバ運用は JST だが API 仕様は時間帯依存しない方が後で多拠点対応する時に楽。

### 1.3 pagination

`list_threads` / `list_events` で対応:

- query params: `limit` (default 100, max 1000), `offset` (default 0)
- response body: `{ items: [...], total: N, limit: ..., offset: ... }`

cursor-based ではなく offset-based。理由:
- chatroom 規模 (~1000s msg) では offset 性能は問題なし
- 後で必要になったら cursor を追加する後方互換は容易 (新 query param `cursor` を accept、`offset` は維持)

### 1.4 エラー format

すべてのエラーレスポンスは以下の形式:

```json
{
  "error_type": "ChatroomIntegrityError",
  "error": "Human-readable message in Japanese or English",
  "details": { /* optional, structured */ }
}
```

| HTTP code | error_type 例 | 説明 |
|---|---|---|
| 400 | `ValidationError` | リクエスト body 形式不正、必須欠落、format 違反 |
| 403 | `ChatroomPermissionError` | close_thread を non-owner が試行、等 |
| 404 | `ChatroomNotFoundError` | thread / msg / project not found |
| 409 | `ChatroomIntegrityError` | invariant 違反 (FK / 重複 / 順序制約) |
| 409 | `ChatroomStateError` | 状態遷移不正 (resolved の再 close 等) |
| 422 | `ValidationError` | pydantic schema validation (FastAPI default) |
| 500 | `ChatroomDBError` | DB エラー |
| 503 | `ServiceUnavailable` | DB 接続不能 (health check で出る) |

### 1.5 URL 設計

- prefix: `/v1/`
- project は path segment: `/v1/projects/{project}/...`
- collection と item: `/v1/projects/{p}/threads` (collection) / `/v1/projects/{p}/threads/{thread_id}` (item)
- action は POST + verb path: `/v1/projects/{p}/threads/{tid}/close` (state-changing custom action)

### 1.6 JSON フィールド命名

すべて **snake_case** (既存 spirrow stack 慣習)。

### 1.7 chatroom_events.action 語彙

write 系 API は対応する action を chatroom_events に append する:

| action | 発生契機 | thread_id | msg_id | details (例) |
|---|---|---|---|---|
| `open_thread` | open_thread API 成功時 | 新 thread | propose msg | `{}` |
| `post_message` | post_message / close_thread API 成功時 | 対象 thread | 新 msg | `{}` |
| `status_transition` | post_message 副作用で thread.status 変化時 | 対象 thread | 引き起こした msg | `{"from": "active", "to": "awaiting_reply"}` |

- close_thread は logically `post_message` + `status_transition` の 2 event を出す (action='close_thread' は使わない、status_transition で `to: 'resolved'` で識別可能)
- 将来 admin 操作 (rename / merge / supersede) を追加する時は新 action を増やす

actor は msg.author を継承 (status_transition の actor も同上)。

---

## 2. リソース schema (response shapes)

すべてのエンティティの完全な JSON 表現:

### 2.1 Thread

```json
{
  "project": "spirrow-voxelworld",
  "thread_id": "T-D4-foo",
  "title": "Foo discussion",
  "owner": "claude.ai",
  "status": "active",
  "created_at": "2026-05-01T19:51:59Z",
  "created_by_msg": "msg-001",
  "resolved_by_msg": null,
  "affects_threads": ["T-OTHER"],
  "tags": ["chatroom-meta", "design"]
}
```

| field | type | 必須 | 備考 |
|---|---|---|---|
| project | string | ✓ | URL path から取得、body には含めない |
| thread_id | string | ✓ | `T-` prefix 推奨 (slug) |
| title | string | ✓ | 1 行説明 |
| owner | string | ✓ | author 文字列、close 権限保有者 |
| status | enum | ✓ | active / awaiting_reply / resolved / superseded / parked |
| created_at | string (ISO 8601 UTC) | ✓ | server-generated |
| created_by_msg | string | ✓ | propose msg の msg_id |
| resolved_by_msg | string \| null | ✗ | resolved 時に decide msg の id |
| affects_threads | string[] | ✗ | default `[]` |
| tags | string[] | ✗ | default `[]` |

### 2.2 Message

```json
{
  "project": "spirrow-voxelworld",
  "msg_id": "msg-001",
  "thread_id": "T-D4-foo",
  "author": "claude.ai",
  "timestamp": "2026-05-01T19:51:59Z",
  "commit_ref": "abc123",
  "type": "propose",
  "content": "...markdown body...",
  "reply_to": null,
  "references_threads": [],
  "related_tasks": [],
  "closes_thread": null,
  "tags": []
}
```

| field | type | 必須 | 備考 |
|---|---|---|---|
| project | string | ✓ | URL path から取得 |
| msg_id | string | ✓ | server-allocated (`msg-NNN` zero-padded 3 桁、繰り上げで 4 桁→5 桁) |
| thread_id | string | ✓ | URL path から取得 |
| author | string | ✓ | claude.ai / claude-code / human / その他自由 |
| timestamp | string (ISO 8601 UTC) | ✓ | client 指定 or server-generated |
| commit_ref | string \| null | ✗ | git hash、任意 |
| type | enum | ✓ | propose / question / answer / decide / report / handoff / ack |
| content | string | ✓ | markdown 本文 |
| reply_to | string \| null | ✗ | 同 thread の msg_id |
| references_threads | string[] | ✗ | 同 project の thread_id |
| related_tasks | string[] | ✗ | format check のみ (実在性は別途) |
| closes_thread | string \| null | ✗ | type=decide とセット、thread.thread_id と一致 |
| tags | string[] | ✗ | default `[]` |

### 2.3 ChatroomEvent

```json
{
  "id": 42,
  "project": "spirrow-voxelworld",
  "timestamp": "2026-05-01T19:51:59Z",
  "actor": "claude.ai",
  "action": "post_message",
  "thread_id": "T-D4-foo",
  "msg_id": "msg-002",
  "details": {}
}
```

| field | type | 必須 | 備考 |
|---|---|---|---|
| id | integer | ✓ | server-allocated, monotonic per cluster |
| project | string | ✓ | path から取得 |
| timestamp | string | ✓ | server-generated |
| actor | string | ✓ | author を継承 |
| action | enum | ✓ | open_thread / post_message / status_transition |
| thread_id | string \| null | ✗ | action に応じて埋める |
| msg_id | string \| null | ✗ | action に応じて埋める |
| details | object | ✓ | action-specific |

### 2.4 IntegrityIssue

```json
{
  "type": "missing_propose",
  "thread_id": "T-D4-foo",
  "msg_id": null,
  "details": "Thread has no propose msg or propose msg.author != owner"
}
```

`type` 一覧:
- `missing_propose` — thread に propose msg なし、or author 不一致
- `closes_thread_by_non_owner` — closes_thread を持つ msg の author != thread.owner
- `invalid_reply_to` — reply_to が同 thread 内に存在しない
- `dangling_thread_reference` — references_threads の対象 thread が project 内にない
- `orphan_message` — msg.thread_id が threads に存在しない (FK が壊れる事故)
- `inconsistent_resolved` — thread.status=resolved だが resolved_by_msg なし、または逆

---

## 3. Endpoint 一覧

### 3.0 GET /health

監視用。

**Response 200:**
```json
{
  "status": "healthy",
  "db": "ok",
  "version": "0.1.0"
}
```

**Response 503** (DB 接続失敗時):
```json
{
  "status": "degraded",
  "db": "error: connection refused",
  "version": "0.1.0"
}
```

---

### 3.1 POST /v1/projects/{project}/threads — open_thread

**Request body:**
```json
{
  "thread_id": "T-D4-foo",
  "title": "Foo discussion",
  "owner": "claude.ai",
  "propose_content": "...markdown...",
  "tags": ["design"],
  "commit_ref": "abc123",
  "timestamp": "2026-05-01T19:51:59Z"
}
```

| field | 必須 | 備考 |
|---|---|---|
| thread_id | ✓ | unique within project |
| title | ✓ | |
| owner | ✓ | propose msg の author としても使われる |
| propose_content | ✓ | propose msg の content |
| tags | ✗ | thread レベル tag |
| commit_ref | ✗ | propose msg にも記録 |
| timestamp | ✗ | propose msg の timestamp、省略時は server now |

**Response 201:**
```json
{
  "thread": { /* Thread */ },
  "msg": { /* Message (the propose msg) */ }
}
```

副作用:
- `chatroom_events` に `open_thread` 1 件 (actor = owner)

**Errors:**
- 409 IntegrityError: thread_id already exists
- 422 ValidationError: required field missing / invalid format
- 500 DBError

---

### 3.2 POST /v1/projects/{project}/threads/{thread_id}/messages — post_message

**Request body:**
```json
{
  "type": "question",
  "author": "claude-code",
  "content": "...",
  "reply_to": "msg-001",
  "references_threads": [],
  "related_tasks": [],
  "closes_thread": null,
  "tags": [],
  "commit_ref": "abc123",
  "timestamp": "2026-05-01T19:51:59Z"
}
```

| field | 必須 | 備考 |
|---|---|---|
| type | ✓ | enum (上記) |
| author | ✓ | |
| content | ✓ | |
| reply_to | ✗ | 同 thread の msg_id |
| references_threads | ✗ | default [] |
| related_tasks | ✗ | default [] |
| closes_thread | ✗ | type=decide でのみ許可、thread_id と一致必須 |
| tags | ✗ | default [] |
| commit_ref | ✗ | |
| timestamp | ✗ | 省略時は server now |

**Response 201:**
```json
{
  "msg": { /* Message */ },
  "thread_status_changed_to": "awaiting_reply"
}
```

`thread_status_changed_to` は status 遷移発生時のみ string、無遷移は `null`。

副作用:
- `chatroom_events` に `post_message` 1 件
- status 遷移発生時、追加で `status_transition` 1 件 (details: {"from": "...", "to": "..."})

**Status transition rules:**
| msg.type | thread.status (before) | thread.status (after) |
|---|---|---|
| handoff | active | awaiting_reply |
| ack | awaiting_reply | active |
| decide + closes_thread | active or awaiting_reply | resolved (resolved_by_msg=msg_id) |
| その他 | (no change) | (no change) |

**Errors:**
- 403 PermissionError: closes_thread セット & author != thread.owner
- 404 NotFoundError: thread_id not found
- 409 IntegrityError: 各種 invariant 違反 (例: propose を既存 thread に post)
- 409 StateError: decide+closes_thread を resolved/superseded/parked thread に
- 422 ValidationError
- 500 DBError

---

### 3.3 POST /v1/projects/{project}/threads/{thread_id}/close — close_thread

`post_message` (type=decide, closes_thread=thread_id) のショートカット。

**Request body:**
```json
{
  "summary_content": "## Resolution: ...\n**結論**: ...",
  "author": "claude.ai",
  "affects_threads": ["T-OTHER"],
  "related_tasks": [],
  "commit_ref": "abc123",
  "timestamp": "2026-05-01T19:51:59Z",
  "tags": []
}
```

**Response 201:**
```json
{
  "thread": { /* Thread (status=resolved, resolved_by_msg set) */ },
  "decide_msg": { /* Message (type=decide) */ }
}
```

副作用:
- `chatroom_events` に `post_message` + `status_transition` の 2 件

**Errors:**
- 403 PermissionError: author != thread.owner
- 404 NotFoundError: thread not found
- 409 StateError: thread.status not in (active, awaiting_reply)

---

### 3.4 GET /v1/projects/{project}/threads — list_threads

**Query params:**
- `status` (multi): `?status=active&status=awaiting_reply` — OR フィルタ
- `owner`: 単一文字列マッチ
- `limit`: int, default 100, max 1000
- `offset`: int, default 0

**Response 200:**
```json
{
  "items": [ /* Thread, Thread, ... */ ],
  "total": 42,
  "limit": 100,
  "offset": 0
}
```

ソート順: `created_at DESC` (新しい thread が先)。

---

### 3.5 GET /v1/projects/{project}/threads/{thread_id} — get_thread

**Query params:**
- `mode`: `full` (default) または `summary`

**Response 200:**
```json
{
  "thread": { /* Thread */ },
  "messages": [ /* Message[] */ ],
  "mode": "full"
}
```

`mode=summary` 時:
- thread.status == 'resolved' なら `messages` には decide msg だけ含まれる (1 件)
- それ以外の status では `mode=full` と同じ全 msg

ソート順: `messages` は `msg_id` 昇順 (post 時刻順)。

**Errors:**
- 404 NotFoundError

---

### 3.6 GET /v1/projects/{project}/events — list_events

**Query params:**
- `thread_id`: filter
- `action`: `open_thread` / `post_message` / `status_transition`
- `since`: ISO 8601 timestamp (inclusive)
- `until`: ISO 8601 timestamp (exclusive)
- `limit`: int, default 100, max 1000
- `offset`: int, default 0

**Response 200:**
```json
{
  "items": [ /* ChatroomEvent[] */ ],
  "total": ...,
  "limit": ...,
  "offset": ...
}
```

ソート順: `timestamp DESC, id DESC` (新しい event が先)。

---

### 3.7 GET /v1/projects/{project}/integrity — check_integrity

**Response 200** (常に 200。違反があっても report として返す):
```json
{
  "issues": [ /* IntegrityIssue[] */ ],
  "issue_count": 3,
  "checked_at": "2026-05-01T19:51:59Z"
}
```

---

## 4. pydantic schema 設計指針

`src/spirrow_conclair/schemas/` 配下:

### 4.1 命名規則

| 用途 | suffix |
|---|---|
| request body | `Request` (例: `OpenThreadRequest`) |
| response body | `Response` (例: `OpenThreadResponse`) |
| エンティティ (DB / 内部) | suffix なし (例: `Thread`, `Message`) |
| list レスポンス wrapper | `ListResponse` (例: `ThreadListResponse`) |

### 4.2 base 設計

- `BaseModel` ではなく `ConfigDict(from_attributes=True, str_strip_whitespace=True)` を持つ共通基底を作る
- timestamp は `datetime` で受ける、シリアライズで `Z` suffix UTC ISO 8601 に正規化
- enum は `Literal[...]` 使用

### 4.3 例

```python
# schemas/thread.py
from typing import Literal
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict

ThreadStatus = Literal['active','awaiting_reply','resolved','superseded','parked']

class Thread(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    project: str
    thread_id: str
    title: str
    owner: str
    status: ThreadStatus
    created_at: datetime
    created_by_msg: str
    resolved_by_msg: str | None = None
    affects_threads: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

class OpenThreadRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    owner: str = Field(min_length=1, max_length=200)
    propose_content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    commit_ref: str | None = None
    timestamp: datetime | None = None

class OpenThreadResponse(BaseModel):
    thread: Thread
    msg: 'Message'  # forward ref

class ThreadListResponse(BaseModel):
    items: list[Thread]
    total: int
    limit: int
    offset: int
```

---

## 5. status code とエラーハンドリング

### 5.1 例外 → HTTP code 変換

`src/spirrow_conclair/main.py` で FastAPI exception handler を定義:

```python
@app.exception_handler(ChatroomNotFoundError)
async def not_found_handler(request, exc):
    return JSONResponse(404, {
        "error_type": "ChatroomNotFoundError",
        "error": str(exc),
        "details": getattr(exc, 'details', None),
    })

@app.exception_handler(ChatroomIntegrityError)
async def integrity_handler(request, exc):
    return JSONResponse(409, ...)

# 同様に Permission, State, DB 各々
```

`ValidationError` (pydantic) は FastAPI 標準の 422 のまま (body を error_type 形式に統一する custom handler を入れる)。

### 5.2 5xx 取扱い

- 500 は予期しない例外を全部キャッチして `ChatroomDBError` で wrap せず raw 500 として返す
- 503 は DB health check が定期的に失敗したら返す (起動直後 DB 未到達など)

---

## 6. 開放しない事項 (v1 スコープ外)

以下は v1 に含めない:

- 認証 / 認可層
- multi-project cross-reference (references_threads は同 project 内に限定)
- thread の rename / merge / supersede 操作
- 添付ファイル
- WebSocket / SSE による real-time push (現状 polling のみ)
- 全文検索 (msg.content への GIN インデックス)

これらは設計 v2 §17 に列挙済。必要時に追加 endpoint を増やす方針。

---

## 7. 検証チェックリスト (T03 以降の確認用)

- [ ] timestamp は server response 時に必ず `Z` suffix UTC
- [ ] 全 422 / 4xx エラーが `error_type` / `error` / `details` 形式
- [ ] pagination の `total` は同フィルタ条件下での全件数
- [ ] msg_id 採番は advisory lock で atomic、連続 post でユニーク
- [ ] status transition は 1 transaction (msg INSERT + thread UPDATE + events INSERT × N)
- [ ] close_thread は decide msg + thread.status='resolved' + status_transition event
- [ ] mode='summary' で resolved thread は decide msg のみ返却
- [ ] check_integrity は違反でも 200 (report endpoint)

---

**End of API Design v1**
