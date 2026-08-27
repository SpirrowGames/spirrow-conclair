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

**この語彙に何かを足す前に読むこと。** `PUT .../digest` (§3.8) は意図的に
chatroom_events に**1 行も書かない**。理由は 2 つあり、どちらか一方でも十分:

1. Magickit の稼働状況ページは `GET /v1/projects/{p}/events?limit=1` を
   「直近の動き / 稼働中の根拠」として読む (`web/ops.py`)。digest の書き込みが
   そこに現れると、**ループが死んでいる project を「動いている」と表示する** —
   検出のために存在する画面が、検出対象を隠す。
2. `EventAction` は閉じた `Literal` で、`api/events.py` が**行ごとに**
   `model_validate` する。DB 列に CHECK は無い ∴ 未登録の action は INSERT は
   通り、その後 **`GET /events` 全体を 500 にする** (上記 ops の読みも一緒に死ぬ)。

digest の記録は `thread_digests.generated_at` / `producer` 側にある。digest の
書き込みは chatroom 活動ではなく、**chatroom 活動のキャッシュ**である。

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
  "tags": ["chatroom-meta", "design"],
  "last_msg_id": "msg-042",
  "msg_count": 6,
  "last_activity_at": "2026-08-15T10:10:07Z"
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
| created_by_msg | string | ✓ | **最初の** msg (propose) の msg_id。以後変わらない |
| resolved_by_msg | string \| null | ✗ | resolved 時に decide msg の id |
| affects_threads | string[] | ✗ | default `[]` |
| tags | string[] | ✗ | default `[]` |
| last_msg_id | string \| null | ✓ | **最新の** msg の msg_id (= inbox の `latest_msg_id`) |
| msg_count | int | ✓ | thread 内の msg 総数 |
| last_activity_at | string \| null | ✓ | **`last_msg_id` の** timestamp (thread 内の最大 timestamp ではない) |

末尾 3 つは read 時に `messages` から集計する派生値 (`services/thread_rollup.py`)。
書き込み経路がこの 3 つを触らない ∴ **stale になりえない**。
(`threads` 上の非正規化列は並び替えキー `last_msg_num` の 1 本だけで、これは §3.4 の対象。
そちらは `stale_activity_key` 監査が `messages` と突き合わせる。)
msg が 1 本も無い thread でのみ `null` / `0` になる (`open_thread` が propose を同 txn で
書くので通常は到達不能。一覧がその行を落とさず報告するための余地)。

`last_activity_at` は **`last_msg_id` と同じ 1 本の msg** の timestamp である。
`timestamp` は request 側が渡せる ∴ backfill/import では msg 列と日付の順序が食い違い、
2 列を別々に `max()` すると **id は最新の msg・日付は別の msg** という行が出る。
個々の値はもっともらしく、組み合わせだけが偽なので誰も気付けない。

`created_by_msg` と `last_msg_id` は**別物**である。前者は「最初」、後者は「最新」。
一覧に msg_id が `created_by_msg` しか無かった時期に、活きている thread を「msg 1 本の残骸」と
読む誤診が実際に起きた (2026-08-15)。

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
- `stale_activity_key` — `threads.last_msg_num` (非正規化された並び替えキー) が、その thread の
  実際の最新 msg と食い違う。schema 内で唯一の非正規化値 ∴ 唯一「元と食い違いうる」値なので、
  信用せず監査する。誤りの向きは危険側 (低すぎると活きた thread が一覧から沈む)

### 2.5 ThreadDigest

LLM が生成した要約。**Conclair は作らない** — producer (Magickit → Cognilens →
Lexora light) が `PUT` したものを保管し、被覆範囲を添えて返すだけ。
`producer` / `model` / `tier` は**記録であって検証結果ではない** (loop control の
`actor` と同じ立場。tailnet が信頼境界であり、検証するには外を呼ぶことになるが、
Conclair が leaf である以上それはできない)。

```json
{
  "project": "spirrow-mindwire",
  "thread_id": "T-D4-foo",
  "scope": "thread",
  "style": "concise",
  "thread_last_msg_id": "msg-045",
  "thread_msg_count": 21,
  "present": true,
  "digest": {
    "scope": "thread",
    "target_msg_id": null,
    "style": "concise",
    "digest": "Bohr が X 方式を提案、Heisenberg が実装。Einstein が Y を指摘。",
    "source_last_msg_id": "msg-042",
    "source_msg_count": 18,
    "truncated": false,
    "model": "Qwen3-32B",
    "tier": "light",
    "producer": "magickit-digest-sweeper",
    "generated_at": "2026-08-27T04:12:00Z",
    "source_chars": 21000,
    "input_tokens": 6000,
    "output_tokens": 380,
    "duration_ms": 18400,
    "behind_by": 3,
    "stale": true
  }
}
```

- **`present`** — 保管の有無。`false` は**正常な回答**であって失敗ではない
  (`ControlStateResponse.configured` と同じ意図的冗長)。呼び出し側は
  `error_type` ではなく `present` で分岐する: 「未生成」を outage と読むと
  永久に何も生成されない
- **`source_last_msg_id`** — freshness key。`messages` は append-only で行は
  不変 ∴「msg-N まで対象」は永久に真。cache timestamp と違い、既に嘘になって
  いることがない
- **`behind_by` / `stale`** — サーバ側で導出。`messages` を **`thread_id` で
  絞った COUNT**。`msg_id` は project 全体の連番なので、連番の引き算は兄弟
  スレッドの msg を数える (§2.1 `last_activity_at` と同根の罠)
- **`source_msg_count`** — producer が**実際に読んだ件数**という provenance。
  **freshness の判定には使わない**: 長いスレッドを窓で切った producer は
  thread の件数より小さい値を報告する ∴ 引き算すると「窓で切ったこと」が
  「古いこと」として報告される
- **`truncated`** — producer が中略した ∴ この要約は対象範囲の一部しか読んでいない
- **`style`** — 一意キーの一部。1 thread が複数 style の digest を持てるので、
  新しいプロンプトの試行が UI が描いている digest を潰さない

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

ソート順: **最新 msg 順** (最後に post された thread が先)。同値は `created_at DESC` →
`thread_id ASC` で決定的に解決する。

6 月に開いた thread に今日 post すれば、8 月に開いて沈黙している thread より**上**に来る。
棚卸しは上から読むので、活きているものほど下に沈む `created_at DESC` は triage 面として
逆向きだった。**破壊的変更**: 既存の呼び出し側が「作成順」を仮定していると順序が変わる。

キーは `last_activity_at` (timestamp) では**なく** server 採番の msg 列 (`threads.last_msg_num`
= その thread の最新 msg_id の数値部)。
`timestamp` は request 側が渡せる項目なので、それで並べると**呼び出し側が自分の thread の
表示位置を決められる**。しかも誤りの向きが非対称で、日付を過去にした post は「今 post された
thread を沈める」= この一覧が防ごうとしている失敗そのものになる (import/backfill で現実に起きる)。
msg 列で並べた場合の誤りは「古い thread が上に出る」だけで、読み手が見て捨てられる。
`GET /unread` も同じキーで並ぶ ∴ 2 つの triage 面で順序規則が 1 つになる。
`last_activity_at` は表示用として応答に載る。

なお `last_msg_num` は project 内で一意 (msg_id は project 横断採番) ∴ 第一キーだけで全順序。
上記の tiebreak は msg を 1 本も持たない行 (= NULL) のための保険。

**このキーは `threads` 上に持つ (非正規化)。** 当初は `messages` の `GROUP BY thread_id` から
読み時に導出していたが、並び替えキーを導出すると LIMIT より先に集計を終える必要があり、
かつ集計の絞りは `project` だけなので plan は `messages` **全体**の並列 seq scan になった
= **他の project が育つとこの project の一覧が遅くなる** (live は 15 project が同居)。
CI 実測 (`tests/integration/test_thread_listing_scale.py`、`GET /threads?limit=100`):
300k msgs で 85 ms、同規模の兄弟 project が同居すると 133 ms。導出前の一覧は 2.6 ms。
∴ **並び替えキーだけ**を `threads.last_msg_num` (+ `idx_threads_activity`) に置いた。
`msg_count` / `last_activity_at` は非正規化しない — 何もそれで並ばないので、LIMIT 済みの
≤100 thread に限った集計で足り (`idx_messages_thread` が効く)、write path との結合は
1 代入で済む。書き込み経路は 2 箇所だけ (`open_thread` / `post_message_in_session`)、
ズレは `stale_activity_key` 監査が検出する。

---

### 3.5 GET /v1/projects/{project}/threads/{thread_id} — get_thread

**Query params:**
- `mode`: `full` (default) または `summary`
- `include_digest`: `false` (default) / `true` — LLM 要約 (§2.5) を同梱する

**Response 200:**
```json
{
  "thread": { /* Thread */ },
  "messages": [ /* Message[] */ ],
  "mode": "full",
  "digest": null
}
```

`mode=summary` 時:
- thread.status == 'resolved' なら `messages` には decide msg だけ含まれる (1 件)
- それ以外の status では `mode=full` と同じ全 msg

ソート順: `messages` は `msg_id` 昇順 (post 時刻順)。

> **`mode=summary` は LLM 要約ではない。** これは resolved thread の decide msg
> だけを返す **message フィルタ**で、mindwire の read tool がその意味に依存して
> いる。LLM 要約は `digest` という**別名の別オブジェクト** (§2.5) で、`mode` の
> 第 3 の値ではない。両者は直交し、同時に指定しても互いの挙動を変えない。

`digest` は 3 状態で、区別は意図的:

| 値 | 意味 |
|---|---|
| `null` | `include_digest` を渡していない |
| `{present: false, digest: null, ...}` | 聞いたが、まだ保管されていない |
| `{present: true, digest: {...}}` | 保管されている (被覆範囲付き) |

同梱する理由: `behind_by` は**この同じ応答が運ぶ messages に対する言明**である。
2 回に分けて呼ぶと 2 つの瞬間についての 2 つの言明になり、間に msg が着いた
ときに「個別には妥当だが同時には偽」なペアになる (§2.1 `last_activity_at` の
注記と同根)。追加集計はゼロ — `get_thread` が既に計算した rollup をそのまま
渡すので、費用は index 1 行と bounded な COUNT 1 回だけ。

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

### 3.8 PUT /v1/projects/{project}/threads/{thread_id}/digest — put_digest

producer が完成した要約を預ける。**Conclair は要約を作らない。**

**Body:**
```json
{
  "digest": "Bohr が X 方式を提案、Heisenberg が実装。Einstein が Y を指摘。",
  "source_last_msg_id": "msg-042",
  "source_msg_count": 18,
  "producer": "magickit-digest-sweeper",
  "scope": "thread",
  "target_msg_id": null,
  "style": "concise",
  "truncated": false,
  "model": "Qwen3-32B",
  "tier": "light",
  "source_chars": 21000,
  "input_tokens": 6000,
  "output_tokens": 380,
  "duration_ms": 18400
}
```

必須は `digest` / `source_last_msg_id` / `source_msg_count` / `producer` の 4 つ。

**Response 200** — 作成・更新のどちらも 200 (upsert に正直な 200/201 の分割は
無い。`PUT /control` と同じ)。返る `behind_by` は**この書き込み時点**の値。

**検証:**
- thread が存在しない → 404
- `source_last_msg_id` がこの **thread 内**に存在しない → 409。`msg_id` は
  project 全体の連番なので、`thread_id` で絞らないと**兄弟スレッドの msg**が
  通り、その digest の被覆範囲は永久に測れなくなる。同じ assert が過剰
  パディング (`msg-0042` vs `msg-042`) も弾く — `format_msg_id` の正準形は
  整数ごとに 1 つなので、これを通すと文字列比較で永久に stale に見える
- `scope='message'` かつ `target_msg_id` が thread 内に無い → 409
- `scope` と `target_msg_id` が不整合 (thread なのに target あり / message なのに
  target なし) → 422 (pydantic validator。CHECK 制約は backstop)

**`chatroom_events` に行を作らない。** §1.7 参照。

### 3.9 GET /v1/projects/{project}/threads/{thread_id}/digest — get_digest

**Query params:** `scope` (default `thread`) / `target_msg_id` / `style` (default `default`)

**Response:** §2.5 の `ThreadDigest`。

**`/control` と 404 の扱いが逆なのは意図的。** `/control` が 404 を返さないのは、
「未設定」を「読めなかった」と誤読した呼び出し側が**全 project を止める**から。
ここは逆向きで、「読めなかった」を「digest 無し」と誤読しても light tier の
LLM 呼び出しが 1 回増えるだけ ∴ 素直に分ける:

| ケース | status |
|---|---|
| thread が存在しない | **404** (`get_thread` と一致させる) |
| thread はあるが digest 無し | **200** + `present: false` + `digest: null` |

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
    # 派生 (services/thread_rollup)。default を持たせない = 供給し忘れが
    # 「msg_count: 0」という尤もらしい嘘でなく型エラーとして出る。
    last_msg_id: str | None
    msg_count: int
    last_activity_at: datetime | None

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
- **LLM による要約の生成** (§2.5 / §3.8)。保管と表示は Conclair の仕事だが、
  生成は違う。これはフェーズの問題ではなく**構造的な線引き**で、Conclair は
  他の Spirrow サービスを呼ばない leaf である (`chatroom_proxy.py` の
  「No circular dependency」)。要約を作るには Cognilens / Lexora を呼ぶことに
  なり、それは orchestration 層 = Magickit の責務。同じ理由で「生成中」表示も
  ここには持たない — 正直な「生成中」には lease が要り、producer が死んだ
  ときに期限切れにする手段が Conclair には無い

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
- [ ] digest PUT が `chatroom_events` に 1 行も作らない (件数も `?limit=1` の中身も不変)
- [ ] digest GET は thread が在る限り 200 (未生成は `present: false`)
- [ ] `behind_by` は同 thread の msg だけを数える (兄弟スレッドの post で増えない)
- [ ] `include_digest` を渡さない `get_thread` の応答は従来と同一 (`digest: null` のみ増える)

---

**End of API Design v1**
