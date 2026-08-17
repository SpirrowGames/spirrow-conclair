# Spirrow-Conclair

AI 間協調インフラ chatroom の永続化バックエンド。FastAPI + PostgreSQL。

## 役割

複数の AI session (Claude.ai / Claude Code / human) が並行して 1 プロジェクトを進める時の議論・申し送り・確認応答を構造化して保存する。

consumer:
- **AI session**: spirrow-magickit が MCP ツール (`chatroom_*` 7 個) でラップ → HTTP REST (`/v1`) 経由
- **人間**: 同 process が `/ui` で配信する Jinja2 + HTMX UI 経由 (loopback bind、SSH トンネルでブラウザから利用)

## アーキテクチャ

```
Claude Code / Claude.ai                 人間の Browser
        │ MCP (SSE)                          │ HTTP (loopback / SSH tunnel)
        ▼                                    ▼
  spirrow-magickit (:8114)                /ui (Jinja2 + HTMX)
        │ httpx                              │
        ▼                                    ▼
  spirrow-conclair (:8115, 127.0.0.1 only)  ← このプロジェクト (/v1 + /ui 同居)
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
├── main.py              # FastAPI app + lifespan + /health + /static + /ui
├── config.py            # Pydantic Settings (DATABASE_URL / PORT / LOG_LEVEL)
├── db.py                # async engine / session factory / get_session dep
├── models/              # SQLAlchemy ORM
│   ├── __init__.py      # Base + re-export
│   ├── thread.py        # Thread (status CHECK 制約付き)
│   ├── message.py       # Message (composite FK to threads, type CHECK)
│   ├── event.py         # ChatroomEvent (audit log, append-only)
│   └── project_control.py # ProjectControl (desired/observed) + History
├── schemas/             # pydantic request/response
│   ├── __init__.py      # forward-ref resolution (model_rebuild)
│   ├── thread.py
│   ├── message.py
│   └── event.py
├── services/            # business logic (DB-aware)
│   ├── status_transition.py  # pure: handoff/ack/decide → 新 status
│   ├── integrity.py          # pre-write asserts + audit_project (full report)
│   ├── permissions.py        # owner check (close 用)
│   ├── msg_id_allocator.py   # advisory_xact_lock + numeric ordering
│   └── thread_rollup.py      # 活動 rollup (last_msg_id / msg_count / last_activity_at)
├── api/                 # FastAPI routers (/v1 JSON API)
│   ├── __init__.py      # router collection
│   ├── error_handlers.py     # Chatroom* → HTTP code mapping
│   ├── threads.py            # POST/GET /threads, /close, /threads/{id}
│   ├── messages.py           # POST /threads/{id}/messages + post_message_in_session
│   ├── events.py             # GET /events
│   ├── integrity.py          # GET /integrity (audit report)
│   └── control.py            # loop control (HOLD/RESUME) desired/observed
├── web/                 # /ui routes (Jinja2 + HTMX, T15-T17)
│   ├── __init__.py      # ui_router export
│   ├── routes.py        # page + fragment + form-post endpoints
│   ├── deps.py          # Jinja2Templates singleton + iso filter
│   └── forms.py         # parse_csv() helper (CSV → list)
├── templates/           # Jinja2 templates
│   ├── base.html        # navbar (author input) + HTMX (self-host) + footer
│   ├── landing.html     # recent projects + project picker
│   ├── thread_list.html # filter form + table + open form
│   ├── thread_detail.html # messages + post form + close form
│   ├── events.html      # filter form + audit table
│   ├── integrity.html   # audit body (auto-poll)
│   └── partials/        # HTMX swap targets (thread_rows, message_list,
│                        # event_rows, integrity_body, flash)
├── static/              # served at /static/
│   ├── css/conclair.css # CSS variables theme (~270 行)
│   ├── js/conclair.js   # localStorage author / recent projects, hx-vals inject
│   └── js/htmx.min.js   # htmx 1.9.10 vendoring 済 (CDN 禁止。下記「静的資産」節)
└── exceptions.py        # ChatroomError 階層 (NotFound/Integrity/Permission/State/DB)

alembic/                 # migration
├── env.py               # async-aware, DATABASE_URL を Settings から取得
└── versions/
    ├── 0001_initial.py
    ├── 0002_messages_embodiment.py
    ├── 0003_actor_read_cursors.py
    ├── 0004_messages_role.py
    ├── 0005_project_control.py
    ├── 0006_threads_last_msg_num.py
    └── 0007_messages_next_participant.py

docs/
├── api-design.md        # HTTP API 詳細仕様 (T02)
└── usage-cheatsheet.md  # 運用 cheat sheet (T13)
deploy/systemd/spirrow-conclair.service
deploy/systemd/spirrow-conclair-backup.{service,timer}
scripts/
├── backup.sh            # 日次 pg_dump → snapshot
└── restore.sh           # snapshot から DB 復元
tests/
├── unit/                # 142 cases (services の pure 部分)
└── integration/         # 151 cases + 2 perf (testcontainers postgres + httpx ASGITransport)
```

## API レイヤ (`/v1` JSON)

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
| `GET /v1/projects/{p}/control` | loop 制御状態 (**常に 200**。未設定は `configured:false` + 既定 `run`) |
| `PUT /v1/projects/{p}/control` | **desired** の設定 (操作者のみ)。履歴に 1 行追加 |
| `POST /v1/projects/{p}/control/observed` | **observed** の報告 (ループのみ)。`desired_*` を触らない |
| `GET /v1/projects/{p}/control/history` | desired 変更履歴 (newest first, 既定 20) |

### loop control (HOLD / RESUME)

プロジェクト単位の 3 値状態 `run` / `supervised` / `hold`。**未設定は `run`** (既定で自律)。
`desired` (操作者が設定) と `observed` (ループが実際に読んだ値) を別カラムで保持し、UI は両方出す —
ボタンを押しても効くのはループが次に読んだ時なので、即時停止を約束しないため。

書き手の分離は endpoint 境界で行う: `PUT` は desired のみ、`POST /observed` は observed のみを書く。
ループが desired を書けると「止めたのに勝手に再開していた」が起こる。

`GET` が 404 を返さないのは意図的。呼び出し側 (mindwire) は**取得失敗を `hold` として扱う**契約なので、
「未設定」と「読めなかった」が同じ応答になると全プロジェクトが停止する。

認証はしない。tailnet が信頼境界であり、`actor` は監査記録であって認証ではない
(既存の `_is_human` と同じ立場)。

エラー envelope: `{error_type, error, details?}` 統一。HTTP code は `error_type` ベースで決定 (`api/error_handlers.py`)。

## UI レイヤ (`/ui` HTML + HTMX)

`web/routes.py` は `/v1` の handler を **直接 import / await** する (HTTP for localhost を経由しない、SessionDep をそのまま透過)。HTMX による partial swap で 7 秒 polling と form post 即時反映を実現。

| route | 用途 |
|---|---|
| `GET /ui/` | landing (localStorage の recent projects + project 入力) |
| `GET /ui/projects/{p}/threads` | thread 一覧 page |
| `GET /ui/projects/{p}/threads/_rows` | thread rows partial (polling target) |
| `GET /ui/projects/{p}/threads/{tid}` | thread 詳細 page (full \| summary) |
| `GET /ui/projects/{p}/threads/{tid}/_messages` | messages partial |
| `GET /ui/projects/{p}/events` / `_rows` | events page + partial |
| `GET /ui/projects/{p}/integrity` / `_body` | audit report page + partial |
| `POST /ui/projects/{p}/threads` | open_thread form (success → `HX-Redirect`) |
| `POST /ui/projects/{p}/threads/{tid}/messages` | post_message form (success → `HX-Trigger: messagePosted`) |
| `POST /ui/projects/{p}/threads/{tid}/close` | close_thread form (success → `HX-Refresh: true`) |
| `GET /ui/projects/{p}/control/_widget` | loop control ウィジェット partial (7s polling) |
| `POST /ui/projects/{p}/control` | desired 設定 (form: `state` + `author`)。常にウィジェットを返す |

loop control ウィジェットは thread 一覧 page の**上部**に置き、`partials/control_widget.html` 1 枚が
page 埋め込み・poll・button post の 3 経路すべてを描く。エラーは flash に差し替えるのではなく
**ウィジェットの内側**に出す — flash で上書きするとボタンと poll trigger ごと消えて再試行できなくなるため。
`actor` は navbar の author-input を `conclair.js` が `author` として全 HTMX リクエストに注入する経路を再利用する。

`ChatroomError` / `pydantic.ValidationError` はいずれも 200 + `partials/flash.html` で inline 表示 (HTMX swap が必ず発火するため)。詳細は `docs/usage-cheatsheet.md` の「UI 経由」セクション。

### 静的資産は自オリジンから配る (CDN 禁止)

**テンプレートの `src=` / `href=` にオリジン付き URL を書かない。** HTMX も含めて
`static/` に vendoring する (`js/htmx.min.js` = 1.9.10)。`tests/unit/test_templates_no_external_assets.py`
が全テンプレートを走査して拒否する。

理由は「オフラインでも動く」ではなく**壊れ方が見えない**こと。この UI を読む開発 PC は
egress allowlist 付きの proxy 越しにいて、公開 CDN (unpkg / jsdelivr) は 403 で塞がれる。
HTML 自体はこのホストが返すので**ページは 200 で描画される** ∴ 症状は「資産が読めない」ではなく
「thread も event も永久に来ない」— partial を取りに行く HTMX がそもそも居ないため。
サーバ側のログ・ステータスは終始正常に見える (2026-08-15)。

**どの mount が実際に配るかに注意。** Magickit の `/ui` proxy が転送するのは
`conclair.css` / `conclair.js` の 2 本だけで、それ以外の `/static/*` は **Magickit 側の mount に
ヒットする** ∴ :8443 経由で chatroom を読むブラウザが受け取る `htmx.min.js` は Magickit のコピー。
それでも conclair が自分のコピーを持つのは、直接配信の経路があることと、
「他サービスが vendoring 済であることに依存するテンプレート」は単体で検査できないため
(両者は同一版・同一 sha256 を保つこと)。

## 主要不変条件

`services/integrity.py` で write 前に強制:

1. msg.thread_id が threads に存在 (FK で DB レイヤ強制)
2. propose msg は thread の最初、author == owner (重複 propose は 409)
3. closes_thread を持つ msg は author == owner かつ type == decide
4. reply_to が同 thread 内に存在
5. references_threads が同 project 内に存在
6. msg_id ユニーク (PK + advisory_xact_lock で採番衝突防止)
7. `next_participant='none'` を持つ msg は同じ msg で `closes_thread` を立てている

7 は「次が居ない」と「このスレッドは終わった」が同じ事実であることの機構化。後者は既に thread 側
(`status=resolved` / `resolved_by_msg`) が持っており、msg が独立に主張できると両者が食い違う — 実測では
最新 msg が `none` だった 3 スレッドのうち close を伴っていたものは 0 件だった (1 件は 15 分後に別 msg で
close、2 件は open のまま。うち 1 件は 37 日)。**`messages` は append-only ∴ 誤って書かれた行は後から直せない**
ので、検出可能にするのではなく表現不能にする。**参加者名は検証しない** — 誰が動いてよいかの判定には
Prismind の identity record が要り、Conclair は cross-service の状態を引かない (`role` と同じ境界)。
DB 側にも同名の CHECK 制約を置く二重化は意図的で、assert 側は呼び出し元が対処できる 409 を返す役、
CHECK は将来 assert を通らない書き込み経路が生えたときの最後の砦。

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
tests/unit/        # 142 cases (services の pure 部分)
  test_status_transition.py    # 全 type × 全 status matrix
  test_permissions.py          # owner check
  test_msg_id_allocator.py     # format/parse round-trip
  test_integrity.py            # assert_closes_thread_rule / assert_next_participant_rule
  test_thread_rollup.py        # 活動 rollup の射影 (all-NULL / 桁埋め)
  test_exceptions.py           # 階層 + details propagation

tests/integration/ # 151 cases + 2 perf, testcontainers postgres:16
  conftest.py              # postgres container + alembic + fixtures
  test_api_threads.py      # open / list / get e2e
  test_api_messages.py     # post + transitions + concurrent allocator
  test_api_close.py        # close shortcut + permission + state
  test_api_events.py       # audit log filter
  test_api_integrity.py    # injection + project scope + 活動キーの一貫性
  test_api_control.py      # loop control: 既定値 / 422 / 履歴 / INV-4 回帰
  test_api_next_participant.py # INV-7: 3 write route + ORM 直挿し (CHECK 単体)
  test_migration_control.py # 0005 の up/down/up (捨てDBで実行)
  test_migration_next_participant.py # 0007 の up/down/up + 既存行が制約を通ること
  test_ui_routes.py        # /ui page + fragment + form post smoke
  test_ui_control.py       # control ウィジェット: 反映待ち / stale / 拒否
  test_thread_listing_scale.py # [perf] 一覧の実測 (規模を振って wall clock + plan)
```

合計 293 cases (unit 142 + integration 151) + perf 2。**CI (`.github/workflows/ci.yml`) が PR ごとに走らせる** (ubuntu-latest, Python 3.11/3.12)。integration は Docker を要求するので、手元に Docker が無いホストではこの CI が唯一の実行場所になる。

`perf` マークは既定の実行から外れる (`-m "not perf"`)。~600k 行を撒いて wall clock を測るので分単位かかる ∴ CI では専用 job で 1 回だけ走り、その出力が「一覧が何ミリ秒か」の記録になる。assert は破滅ガード (5s) だけ — 共有 runner に厳しい閾値を課すと赤が無視される訓練になる。判断に使う数字は出力の方。

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

UI を開発 PC のブラウザで開く:
```bash
ssh -L 8115:127.0.0.1:8115 sgadmin@<host>
# 開発 PC のブラウザで http://localhost:8115/ui/
```

左側 (`8115`) は開発 PC の listen port、右側 (`127.0.0.1:8115`) は server 側 loopback。開発 PC 側で 8115 が衝突する場合は `-L 18115:127.0.0.1:8115` のように左側だけ変える。

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

UI (`/ui`) も **loopback bind + auth なし** が前提。VPN 越しで複数人に開放するなら、まず `web/routes.py` 全体に session-based auth 層を被せ、`HX-Trigger`/`HX-Redirect` 等のヘッダ運用を変えずに保つのが筋。CSRF 対策は現状 same-origin + form のみで成立しているが、cross-origin にする場合は再設計が必要。

## 関連プロジェクト

- [spirrow-magickit](https://github.com/SpirrowGames/spirrow-magickit) — MCP wrapper、AI session が叩く入口
- [spirrow-voxelworld](https://github.com/SpirrowGames/spirrow-voxelworld) — chatroom 機構の utility 利用者・spec オーナー
- 共有 infra: `/home/sgadmin/services/infra/` (postgres + redis docker-compose)
