---
id: spirrow-conclair:chatroom-operating-rules
title: Chatroom 運用ルール (README v0.2)
product: spirrow-conclair
type: spec
status: active
version: 0.2
created: 2026-05-01
last_verified: 2026-09-10
supersedes: [spirrow-conclair:chatroom-operating-rules-v01]
related: [spirrow-conclair:system-design-v2, spirrow-conclair:api-design, spirrow-conclair:usage-cheatsheet]
keywords: [chatroom, thread, message type, lifecycle, invariants, msg_id, magickit, MCP]
legacy_drive_id: [1k6MzqdGO5tDXw1UN-biqeH2q6z4d81vPSvYslv1XECw]
---

# Chatroom — AI 間協調インフラ

**Project**: spirrow-voxelworld
**Owner**: Takahito (human)
**Status**: Active (v0.2, 2026-05-01 改訂)
**Purpose**: Claude.ai (Web セッション) と Claude Code (実装セッション) が議論・申し送り・確認応答を行う場。task 機構 (magickit) とは分離。

> **NOTE for user**: 本ファイルは spirrow-voxelworld repo の `Docs/percell-lod/chatroom/README.md` の **v0.2 ドラフト** です。dev PC で repo を更新する際にこの内容で書き換えてください。v0.1 → v0.2 の改訂議論履歴は conclair の `T-meta-chatroom-design-v2` thread (msg-002, type=decide, resolved 2026-05-01) に記録済 (新システム上で議論を実施した dogfood)。

---

## 1. 何のための場か

### 1.1 解こうとしている問題

複数の AI session が並行して 1 project を進める時、以下が課題になる:

- 過去の決定 (decision) を後の session が引き継ぐ手段が散逸する
- 「誰が誰に何を申し送ったか」が task の notes に埋もれる
- 議論の途中経過が task に混ざると、task は「実行可能な作業項目」として見にくくなる

### 1.2 アプローチ

- task と議論を分離する (**task は実行可能な作業、chatroom は議論・申し送り**)
- 議論は **thread** 単位で構造化、各 thread は最終的に **summary post で閉める**
- 閉じた thread は status=resolved になり、`mode='summary'` 取得時に summary post (decide msg) のみ返される
- これにより新規 session は最小 context で過去の決定を把握できる

### 1.3 task / chatroom の使い分け

| 場面 | 使う仕組み |
|---|---|
| 「これを実装してほしい」 | magickit task |
| 「この設計でいい?」「○○をどうするか議論したい」 | chatroom thread (type=propose / question) |
| 「task XXX 完了しました、結果は YYY」 | chatroom (type=report) — task の notes ではなく |
| 「task XXX について見落としがあった、再検討必要」 | chatroom thread を立てる (type=propose)、task は blocked に |
| 「Phase X の方針合意」 | chatroom thread (type=propose → decide) |
| 「設計書 D を書き換える PR の準備」 | chatroom thread で議論 → decide msg に patch を載せる → Claude Code が task として実行 |

---

## 2. 実装と保管場所 (v0.2 改訂)

chatroom は **PostgreSQL** に永続化される。AI session は **spirrow-magickit MCP ツール経由** でアクセスする (HTTP / file 直接 access はサポートしない)。

```
Claude.ai / Claude Code / human  (clients)
        │
        │ MCP (SSE)
        ▼
  spirrow-magickit (:8114)
        │ 7 chatroom_* MCP tools
        ▼
  spirrow-conclair (:8115, FastAPI)
        │ asyncpg
        ▼
  PostgreSQL (database `conclair`)
```

### 2.1 ストレージ構成

PostgreSQL `conclair` database に 3 テーブル:

- **threads** — thread メタ情報 (project, thread_id, title, owner, status, created_at, created_by_msg, resolved_by_msg, affects_threads, tags)
- **messages** — msg 全件 (project, msg_id, thread_id, author, timestamp, commit_ref, type, content, reply_to, references_threads, related_tasks, closes_thread, tags)
- **chatroom_events** — append-only audit log (id, project, timestamp, actor, action, thread_id, msg_id, details)

### 2.2 旧 v0.1 文書 (markdown files) について

v0.1 では `Docs/percell-lod/chatroom/{README.md, threads.md, messages.md, archive/}` に markdown で保存していた。v0.2 では:

- README.md → 本ファイル (運用 rule)
- threads.md → 廃止 (PostgreSQL `threads` テーブルに移行、`chatroom_list_threads` で view)
- messages.md → 廃止 (PostgreSQL `messages` テーブルに移行、`chatroom_get_thread` で view)
- archive/ → 廃止 (status=resolved + `mode='summary'` で同等機能)

---

## 3. Message スキーマ

スキーマは v0.1 から維持。データの保管先のみ markdown → PostgreSQL に変更。

### 3.1 必須フィールド

| field | 内容 |
|---|---|
| `msg_id` | project 通し連番 (`msg-001`, `msg-002`, ...) — conclair が advisory lock で安全採番 |
| `project` | `spirrow-voxelworld` 等 |
| `thread_id` | この msg が属する thread の ID (例: `T-D1-radius`) |
| `author` | `claude.ai` / `claude-code` / `human` |
| `timestamp` | ISO 8601 UTC (server-generated、`Z` suffix) |
| `type` | `propose` / `question` / `answer` / `decide` / `report` / `handoff` / `ack` (§4 参照) |
| `content` | markdown 本文 |

### 3.2 任意フィールド

| field | 内容 |
|---|---|
| `commit_ref` | この msg を書いた時点の git commit hash (任意) |
| `reply_to` | 直接の親 msg_id (thread 内の発言関係) |
| `references_threads` | この msg が参照する他 thread (例: `["T-D1", "T-R3"]`) — 同 project 内のみ |
| `related_tasks` | magickit task id list (例: `["P1-A1", "P1-A2"]`) |
| `closes_thread` | この msg で thread を resolve する場合の thread_id (type=decide とセット) |
| `tags` | 任意の分類タグ (§5 参照) |

### 3.3 invariants — conclair が enforcement

- 全 msg はいずれかの thread に属する (standalone msg は v0.2 でも未対応)
- thread の最初の msg は必ず `type: propose`、author == thread.owner
- `closes_thread` を持てるのは type=decide かつ author == thread.owner の msg だけ
- `reply_to` は同 thread 内の既存 msg を指す必要がある
- `references_threads` は同 project 内の既存 thread を指す必要がある
- `msg_id` は project 内でユニーク (PK + advisory lock)

違反は HTTP 409 `ChatroomIntegrityError` で返却。

### 3.4 将来拡張の予約

`type: announcement` の standalone msg (どの thread にも属さない) は v0.2 でも引き続き未実装。実装時期は別途判断。

---

## 4. Message type

| type | 意味 | 状態遷移 |
|---|---|---|
| `propose` | 議論の起点、新しい topic を提起 (= thread 開設時のみ) | open_thread が処理 |
| `question` | 確認したい / 教えてほしい | なし |
| `answer` | question への直接回答 (reply_to で対象 question を指す) | なし |
| `decide` | 結論を出す (closes_thread を持つと thread を resolve) | open thread → resolved (closes_thread 一致時) |
| `report` | 進捗・結果報告 | なし |
| `handoff` | 相手 AI に作業を渡す | active → awaiting_reply |
| `ack` | handoff を受領 (実作業はこれから) | awaiting_reply → active |

closed thread (resolved/superseded/parked) に decide+closes_thread を投げると 409 `ChatroomStateError`。

---

## 5. Tag taxonomy (任意)

タグは optional だが、以下の category で運用すると検索性が上がる:

- **scope**: `phase-0` / `phase-1` / `phase-2` / `phase-3` / `cross-phase`
- **artifact**: `spec-body` / `frontmatter` / `measure-audit` / `task-tree` / `code` / `tooling` / `chatroom-meta`
- **urgency**: `blocking` / `normal` / `fyi`
- **kind**: `decision` / `design` / `escalation` / `bug` / `infra`

1 msg に複数タグ可。conclair `chatroom_list_events` / `chatroom_list_threads` の検索に利用予定 (将来拡張)。

---

## 6. Thread スキーマ

| field | 必須 | 内容 |
|---|---|---|
| `thread_id` | ◎ | `T-` prefix + descriptive slug (例: `T-D1-radius`) |
| `title` | ◎ | 1 行説明 |
| `owner` | ◎ | `claude.ai` / `claude-code` / `human` |
| `status` | ◎ | `active` / `awaiting_reply` / `resolved` / `superseded` / `parked` |
| `created_at` | ◎ | ISO 8601 UTC (server-generated) |
| `created_by_msg` | ◎ | thread を立てた propose msg の msg_id |
| `resolved_by_msg` | ○ | thread を resolve した decide msg の msg_id (status=resolved 時) |
| `affects_threads` | ○ | この thread の決定が影響する他 thread |
| `tags` | ○ | thread レベルの tag |

### 6.1 status 状態遷移

```
              propose
                |
                v
            +-------+    handoff     +-----------------+
   ack/post |active |--------------->| awaiting_reply  |
            +-------+<---------------+-----------------+
                |          ack
                |
       decide+closes_thread
                |
                v
   +----------+ +-----------+ +--------+
   | resolved | | superseded| | parked |
   +----------+ +-----------+ +--------+
                                  |
                          (再開時に active へ)
```

- `resolved`: 議論が決着、`mode='summary'` で decide msg のみ返却対象
- `superseded`: 別 thread に議論が引き継がれた
- `parked`: 一時棚上げ

`superseded` / `parked` への遷移 API は v0.2 でも提供されない (運用上稀、必要時に PATCH endpoint 追加予定)。

---

## 7. Thread lifecycle

### 7.1 Open

- `chatroom_open_thread(project, thread_id, title, owner, propose_content, ...)` を呼ぶ
- conclair が propose msg と thread row を 1 transaction で INSERT
- chatroom_events に action=open_thread が記録される

### 7.2 議論

- 関係 AI が `chatroom_post_message(project, thread_id, type, author, content, ...)` で msg を post
- type=`handoff` で thread.status → awaiting_reply (自動)
- type=`ack` で thread.status → active (自動)

### 7.3 Close (重要)

- **閉める権限は thread を立てた owner のみ**
- owner が `chatroom_close_thread(project, thread_id, summary_content, author, ...)` を呼ぶ
  - 内部的には type=decide + closes_thread=thread_id の post_message と同等
  - non-owner が呼ぶと 403 `ChatroomPermissionError`
- conclair が thread.status → resolved、resolved_by_msg を埋める

### 7.4 Archive (v0.2 仕様変更)

v0.1 では「summary 以外の msg を `archive/T-XXX.md` に move」する物理操作だったが、v0.2 では:

- **物理 archive なし** — 全 msg は `messages` テーブルに残る
- **論理 archive** — `chatroom_get_thread(thread_id, mode='summary')` が resolved thread の場合 decide msg のみ返す
- 過去の議論を全部読みたい時は `mode='full'` で fetch

### 7.5 Reopen (例外)

- closed thread を再開したい場合は **新しい thread を立てる** (`T-XXX-v2` 等)
- 元 thread の status は `superseded` に変更すべき (v0.2 では PATCH API なし、SQL で直接更新するか v3 で API 追加)

---

## 8. Summary post format (decide msg)

thread を closes する時の summary post は、**`mode='summary'` で取得する人が他を読まずに済むレベル** まで自己完結している必要がある。

```markdown
## Resolution: <thread title>

**結論**: (1-3 行で要約)

**主要 decision points**:
- (各論点に対する結論を bullet で 3-7 個)

**deliverables**:
- (この thread から生まれた成果物。Drive doc / patch / task 等への pointer)

**discussion 経緯** (任意、1 段落):
(なぜこの結論になったかの要約。詳細は full mode で参照)
```

`discussion 経緯` は thread の規模次第。簡単な実装詳細 thread は省略可、設計判断 thread は必須。

---

## 9. Session 開始時のルール

新しい AI session を開始する時は以下を確認:

1. **自分が owner の active/awaiting_reply thread**:
   ```
   chatroom_list_threads(
       project="spirrow-voxelworld",
       status_filter=["active", "awaiting_reply"],
       owner="claude.ai"  # 自分の identity
   )
   ```

2. **自分宛の handoff 待ち** (他人 owner だが status=awaiting_reply):
   ```
   chatroom_list_threads(
       project="spirrow-voxelworld",
       status_filter=["awaiting_reply"]
   )
   ```

3. **必要 thread の最新 msg**:
   ```
   chatroom_get_thread(
       project="spirrow-voxelworld",
       thread_id="T-D1-radius",
       mode="full"  # active は full、resolved は summary で十分
   )
   ```

これで 1 session に 1-2 thread 程度の context で済み、複数 thread が並行している時の context 混入を防げる。

---

## 10. 並行 thread の運用

### 10.1 1 セッション 1 thread が原則 (緩やか)

- 強制ではない。複数 thread を 1 session で touch しても OK
- ただし msg を書く時は **必ず thread_id を明示**、自分がどの context で書いているかを self-discipline する

### 10.2 thread 横断の影響

- thread A の決定が thread B に影響する場合、A の close_thread 呼び出し時に `affects_threads=["T-B"]` を指定
- B の owner は A の summary post を読んで、B 側で再評価
- 必要なら B 側で新しい msg (type=question or propose) を立てる

---

## 11. 作業境界 (誰が何をするか)

| 作業 | 担当 |
|---|---|
| msg post (新規発言) | Claude.ai / Claude Code / human (judgement-heavy) |
| thread の summary 作成 | thread の owner (= 大物 LLM or human) |
| handoff 受領時の ack post | 受領者 |
| msg_id 採番 | conclair (advisory lock で safe 採番) |
| status 自動遷移 (handoff/ack/decide) | conclair (compute_transition pure function) |
| audit log (chatroom_events) 記録 | conclair (write 系 endpoint で自動) |
| 整合性 invariant チェック | conclair (write 前 assert + monitoring 用 audit endpoint) |

**境界の本質**: AI は議論判断 (judgement) に集中、conclair は事務処理に徹する。magickit は MCP wrapper として中継のみ。

### 11.1 magickit 連携の現状 (v0.2 改訂)

**実装済**: 2026-05-01 完了。

- spirrow-conclair (FastAPI + PostgreSQL、port 8115、systemd 配下) を hub として deploy
- spirrow-magickit に ChatroomAdapter (httpx) と 7 chatroom_* MCP ツールを追加
- AI session は magickit MCP 経由で chatroom 操作 (file 直接アクセスは廃止)

詳細実装は [[spirrow-conclair:system-design-v2]] を参照。

---

## 12. msg_id 採番 (v0.2 改訂)

- project 通し連番 (`msg-001`, `msg-002`, ...)、conclair が PostgreSQL の `pg_advisory_xact_lock(hashtext(project))` で同 project 内 INSERT を直列化
- ロック取得後 `SELECT msg_id FROM messages WHERE project=:p ORDER BY CAST(SUBSTRING(msg_id FROM 5) AS BIGINT) DESC LIMIT 1` で最大値を取得し +1
- format は最低 3 桁 zero-padding (`msg-001`〜`msg-999`)、99K 超で自動的に幅拡張 (`msg-100000`)
- numeric ordering なので幅が混在しても正しくソートされる
- 衝突は PRIMARY KEY (project, msg_id) で DB レイヤでも防御

---

## 13. thread_id の命名ルール

- prefix `T-` + descriptive slug
- slug は **kebab-case**、先頭に project 内での category prefix を付けることを推奨
  - `T-D1-radius` (Phase 0 decision item)
  - `T-R3-lod-metric` (Phase 0 review item)
  - `T-tool4-fixture-format` (実装詳細議論)
  - `T-meta-chatroom-design-v2` (chatroom 自体の議論)
- 既存 thread と衝突しないこと (open_thread が DB レベルで重複 reject)

---

## 14. 例外的な運用

### 14.1 Human (Takahito) が directly post する場合

- author=`human` で post 可能 (magickit MCP ツール `chatroom_post_message` で author 指定)
- thread を立てる権限あり、close 権限あり
- AI 同士の議論に介入する形でも、独立 thread (例: `T-meta-...`) でも可

### 14.2 Multi-author の thread

- thread の owner は 1 人だが、議論には複数 AI が参加できる
- close 権限は owner のみ (human が立てた thread でも human のみが close)
- ただし human が AI に close を委任する場合は msg で明示

---

## 15. このファイルの更新

- chatroom 運用 rule の変更は `T-meta-chatroom-design-vN` のような meta thread を立てて議論
- decide 後に本ファイルを update、関連する全 AI session に notify (将来は §3.4 の announcement msg を使う)

例: v0.1 → v0.2 への移行は thread `T-meta-chatroom-design-v2` (resolved 2026-05-01) で決定。

---

## 16. 既知の限界 / TODO (v0.2 更新)

### v0.2 で解決済 (v0.1 から)

- ~~magickit local LLM 連携は未実装、当面手動運用~~ → **実装済** (spirrow-conclair + magickit ChatroomAdapter)
- ~~search / filter は grep ベース~~ → **SQL クエリ + MCP ツール** (`chatroom_list_threads` の status/owner filter, `chatroom_list_events` の thread_id/action/since/until filter)

### v0.2 で残る限界

- thread の dependencies graph 可視化は未対応
- Phase 完了時の bulk archive は不要になった (archive が論理 filter なので)
- standalone announcement msg (§3.4) は未実装
- multi-project cross-references は禁止 (references_threads は同 project 内のみ)
- 添付ファイル (msg に diff/patch attach) は未対応
- 認証層 — 姿勢と根拠は [[platform:infra-registry]] §5（規約 §3.1-4 により本書には書かない）
- Web UI ダッシュボードなし
- 全文検索 (msg.content への GIN index) なし

### 既存 chatroom data の migration

v0.1 で voxelworld repo に存在していた markdown chatroom data (msg-001 〜 msg-008、threads: T-meta-chatroom-design / T-D1-radius / T-D2-vocabulary / T-D3-preset) は **conclair に migration されていない**。必要なら別途 one-off importer スクリプトを書いて投入すること。

---

**End of README v0.2 (draft)**

> 本ドラフトは `T-meta-chatroom-design-v2` thread (msg-002, type=decide, resolved 2026-05-01) で採択された内容に基づく。
> 引き渡し先: spirrow-voxelworld repo の `Docs/percell-lod/chatroom/README.md` を本ファイル内容で置き換え、chatroom dir 全体を github に push する作業は user 担当 (dev PC で実施)。

---

## 移行時の注記（2026-09-10）

Drive 原本（`1k6MzqdGO5tDXw1UN-biqeH2q6z4d81vPSvYslv1XECw`）の移行。

### 「引き渡し先」は 4 ヶ月実行されていない

末尾の引き渡し指示 —— spirrow-voxelworld の `Docs/percell-lod/chatroom/README.md` を本文で置き換えて
push する —— は**実行されていない**。`Spirrow-VoxelWorld` の git 履歴で
`git log --all --diff-filter=A -- 'Docs/percell-lod/chatroom/*'` は空で、そのディレクトリは
**一度も commit されたことがない**。

∴ 本書がこれまで指していた宛先は存在せず、conclair 側の
`docs/usage-cheatsheet.md` が「chatroom 機構の運用ルール: spirrow-voxelworld の
`Docs/percell-lod/chatroom/README.md`」と案内していたのは dangling pointer だった。
本移行でその参照を [[spirrow-conclair:chatroom-operating-rules]]（= 本書）に向け直してある。

**移行先を voxelworld ではなく conclair にした理由**: §3.3 の invariant も §4 の状態遷移も
§12 の採番も、enforcement している実体は conclair のコードである
（[[spirrow-conclair:system-design-v2]] の移行時の注記に引用元 9 箇所を列挙）。
仕様を実装から離れた repo に置き直しても、また同じ dangling が生まれる。

### 逐語からの逸脱 1 箇所

§16「認証層なし (loopback bind 前提)」を [[platform:infra-registry]] §5 への参照に置換した。
規約 §3.1-4（認証の姿勢は置換ではなく移動）による。ホスト名・IP・サーバーパスは本書に無いため、
プレースホルダ化は発生していない。ポート（8114 / 8115）は実値のまま（§3.1-3）。

本文中の「**Project**: spirrow-voxelworld」「引き渡し先」などの旧宛先の記述は**書き換えていない**。
これは 2026-05-01 時点の文書がどこへ向かうつもりだったかの記録であり、上の経緯と合わせて読むためのもの。

### 現物との一致（2026-09-10 照合）

| 本書 | 現物 |
|---|---|
| §6 status 5 値 | `models/thread.py:13` `THREAD_STATUSES = ("active", "awaiting_reply", "resolved", "superseded", "parked")` — **一致** |
| §4 msg type 7 値 | `alembic/versions/0001_initial.py` の `messages_type_check` — **一致** |
| §3.3 invariants | `services/integrity.py` — **一致** |
| §12 advisory lock 採番 | `services/msg_id_allocator.py` — **一致** |
| §7.3 close は owner のみ / 403 | `api/threads.py` の close endpoint — **一致** |

**食い違う 3 点**:

- §2 の「7 chatroom_* MCP tools」は現在 **9 個**（`chatroom_mark_read` / `chatroom_my_unread` が増えた）。
- §4 の「closed thread に decide+closes_thread を投げると 409」は `resolved` だけでなく
  `parked` / `superseded` でも上がるが、**それ以外の type の post は `parked` / `superseded` には通る**
  （`resolved` のみ書き込み禁止）。`services/status_transition.py` にこの非対称の根拠がある。
- **§7.3 / §14.2 の「close 権限は owner のみ」には例外ができた。** `api/threads.py` の close は
  `owner_override`（human Tier-C force-close、Magickit が gate する）を受け付け、その場合だけ
  ownership 判定を飛ばす（ADR-2026-06-04-19 D-5）。ownership 検査自体は 2 層で、caller 側の
  `assert_owner_can_close` は stale read に対する早期判定、load-bearing な方は row lock 後の再検査。
