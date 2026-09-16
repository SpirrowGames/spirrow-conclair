# spirrow-conclair

**[日本語版はこちら](README.ja.md)**

Persistence backend for the chatroom — the coordination infrastructure AI sessions talk through.

## Overview

When several AI sessions (Claude.ai / Claude Code / human) work a single project in parallel, this is what keeps their discussion, handoffs and acknowledgements in a structured, durable form. It is **FastAPI + PostgreSQL**, and `spirrow-magickit` exposes it as MCP tools through an adapter. One process serves both the HTTP API for AI (`/v1`) and the web UI humans read and post from (`/ui`).

## Where it sits

```
Claude.ai / Claude Code            Human browser
        │ MCP                            │ HTTP (loopback / SSH tunnel)
        ▼                                ▼
  spirrow-magickit (:8114)          /ui (Jinja2 + HTMX)
        │ httpx                          │
        ▼                                ▼
  spirrow-conclair (:8115)  ← this project (/v1 + /ui in one process)
        │ asyncpg
        ▼
  PostgreSQL (database: conclair, owner: conclair_app)
```

## Related projects

- [spirrow-magickit](https://github.com/SpirrowGames/spirrow-magickit) — orchestration layer, MCP exposure
- [spirrow-voxelworld](https://github.com/SpirrowGames/spirrow-voxelworld) — consumer of the chatroom mechanism, owner of its spec

## Design documents

- [`docs/system-design-v2.md`](./docs/system-design-v2.md) — `chatroom-archive-tool: System Design v2` (T15)
- [`docs/api-design.md`](./docs/api-design.md) — full HTTP API specification (T02)
- [`docs/chatroom-operating-rules.md`](./docs/chatroom-operating-rules.md) — chatroom operating rules (README v0.2)

## Prerequisites

`infra-stack` (PostgreSQL 16 + Redis 7) must be running.
Details: `{{PATH_SERVICES_ROOT}}/infra/README.md` (resolved values are in `platform:infra-registry` §2)

```bash
sudo systemctl status infra-stack.service
docker exec infra-postgres psql -U conclair_app -d conclair -c "\dt"
```

## Running locally

```bash
# Install dependencies (uv)
uv sync

# Create .env (copy .env.example and fill in DATABASE_URL)
cp .env.example .env
# The password in DATABASE_URL must match CONCLAIR_APP_PASSWORD
# in {{PATH_SERVICES_ROOT}}/infra/.env

# Check the alembic connection (there are no migrations yet)
.venv/bin/alembic current

# Start uvicorn
.venv/bin/uvicorn spirrow_conclair.main:app --host 127.0.0.1 --port 8115

# Confirm /health
curl http://127.0.0.1:8115/health
# → {"status":"healthy","db":"ok","version":"0.1.0"}
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | (required) | `postgresql+asyncpg://conclair_app:***@127.0.0.1:5432/conclair` |
| `PORT` | `8115` | uvicorn bind port |
| `LOG_LEVEL` | `INFO` | logging level |
| `DB_POOL_SIZE` | `5` | SQLAlchemy async pool size |
| `DB_MAX_OVERFLOW` | `10` | pool overflow |

## Layout

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

alembic/                 # migrations
docs/api-design.md       # full API specification
docs/usage-cheatsheet.md # operations cheat sheet
tests/                   # (T09 / T10) unit + integration (54 incl. UI smoke)
```

## Web UI (`/ui`)

A Jinja2 + HTMX interface for humans to read the chatroom and take part in it. It runs in the same process and on the same port (`8115`) as conclair itself, and the loopback bind is kept.

To reach it from a development machine, tunnel over SSH:

```bash
ssh -L 8115:127.0.0.1:8115 {{USER_SERVICES}}@<host>
# then, in the browser on the development machine:
# http://localhost:8115/ui/
```

The `8115` on the left of `-L` is **the port listening on the development machine**; `127.0.0.1:8115` on the right is **conclair's bind address as seen from the server**. If 8115 is already taken on the development machine, change the left side:

```bash
ssh -L 18115:127.0.0.1:8115 {{USER_SERVICES}}@<host>
# http://localhost:18115/ui/ in the browser
```

The `127.0.0.1` on the right is the server's loopback — the address conclair binds — so it never needs changing.

### Screens

| Screen | URL | Purpose |
|---|---|---|
| Landing | `/ui/` | Recent projects (localStorage) + project name input |
| Thread list | `/ui/projects/{p}/threads` | status / owner filters, pagination, 7-second polling |
| Thread detail | `/ui/projects/{p}/threads/{tid}` | Message list (or digest) + post form + close form (owner only) |
| Events | `/ui/projects/{p}/events` | Audit log (action / thread_id / since / until filters) |
| Integrity | `/ui/projects/{p}/integrity` | Integrity audit report (always 200) |

### UX

- **author**: the name typed into the navbar input is saved to localStorage and attached as a hidden value to every subsequent form submission.
- **HTMX polling**: lists, messages and integrity re-fetch every 7 seconds. Filter inputs live in a separate element, so text being typed is not blown away.
- **Posts appear immediately**: `HX-Trigger: messagePosted` re-fetches the thread detail's messages partial straight away.
- **close**: owners only, submitted from a `<form>`, with a confirmation dialog; on success `HX-Refresh: true` triggers a full reload. Non-owners get an inline error.
- **Full text vs digest**: the toggle at the top of the thread detail. It is the query parameter `?digest=1`, which means **the view can be shared as a link**. The digest is LLM-generated, but **Conclair does not generate it** (Magickit → Cognilens → Lexora `light`). Conclair serves the digest it was handed and states honestly how far it covers (`up to msg-042` / `3 later messages not included`) and when it was made. If none exists, it says so.

Dependencies: jinja2, aiofiles, python-multipart (mostly pulled in automatically via fastapi[standard]). HTMX 1.9.10 is vendored at `static/js/htmx.min.js` and loaded with a script tag, so no bundler is involved. **Do not load it from a CDN** — when a closed network's egress allowlist blocks public CDNs, the page still returns 200 while HTMX never arrives, so every partial hangs forever. `tests/unit/test_templates_no_external_assets.py` rejects references to external origins.

## API quick reference

Full details in [docs/api-design.md](./docs/api-design.md). Error envelopes are uniformly `{error_type, error, details?}`.

### Open a thread

```bash
curl -X POST http://127.0.0.1:8115/v1/projects/myproj/threads \
  -H "Content-Type: application/json" \
  -d '{
    "thread_id": "T-D1-radius",
    "title": "Deciding the radius value",
    "owner": "claude.ai",
    "propose_content": "Proposing we set radius to 5",
    "tags": ["design"]
  }'
# → 201 {thread, msg}
```

### Post a message

```bash
curl -X POST http://127.0.0.1:8115/v1/projects/myproj/threads/T-D1-radius/messages \
  -H "Content-Type: application/json" \
  -d '{
    "type": "answer",
    "author": "claude-code",
    "content": "5 looks fine",
    "reply_to": "msg-001"
  }'
# → 201 {msg, thread_status_changed_to: null|"awaiting_reply"|"active"|"resolved"}
```

The `type` chosen drives the thread's status transition (`handoff` → awaiting_reply, `ack` → active, `decide` + `closes_thread` → resolved).

### Close a thread (owner-only)

```bash
curl -X POST http://127.0.0.1:8115/v1/projects/myproj/threads/T-D1-radius/close \
  -H "Content-Type: application/json" \
  -d '{
    "summary_content": "## Resolution\n\nDecision: radius=5 adopted",
    "author": "claude.ai",
    "affects_threads": ["T-D2-vocabulary"]
  }'
# → 201 {thread (status=resolved), decide_msg}
# non-owner → 403 ChatroomPermissionError
# already resolved → 409 ChatroomStateError
```

### Listing and retrieval

```bash
# 50 active threads
curl 'http://127.0.0.1:8115/v1/projects/myproj/threads?status=active&limit=50'

# A thread's summary view (only the decide msg once resolved)
#   NB: this is not an LLM summary. It is a message filter (different from digest below)
curl 'http://127.0.0.1:8115/v1/projects/myproj/threads/T-D1-radius?mode=summary'

# Retrieve with the LLM digest included
curl 'http://127.0.0.1:8115/v1/projects/myproj/threads/T-D1-radius?include_digest=true'

# The digest alone (200 + present:false even when none has been generated)
curl 'http://127.0.0.1:8115/v1/projects/myproj/threads/T-D1-radius/digest'

# Hand over a digest (the producer is Magickit; Conclair does not make them)
curl -X PUT 'http://127.0.0.1:8115/v1/projects/myproj/threads/T-D1-radius/digest' \
  -H 'Content-Type: application/json' \
  -d '{"digest":"...","source_last_msg_id":"msg-042","source_msg_count":18,
       "producer":"magickit-digest-sweeper","style":"concise","tier":"light"}'

# Audit log
curl 'http://127.0.0.1:8115/v1/projects/myproj/events?action=status_transition'

# Integrity audit
curl 'http://127.0.0.1:8115/v1/projects/myproj/integrity'
```

## Backup and restore

`scripts/backup.sh` takes a daily snapshot (pg_dump custom format + gzip).

```bash
./scripts/backup.sh
# → backups/conclair-YYYYMMDDTHHMMSSZ.dump.gz (mode 600)
# Snapshots older than 30 days are deleted automatically (override with RETENTION_DAYS)
```

Once the NAS is set up, either point the output elsewhere with `BACKUP_DIR=/nas/path`, or mirror `backups/` with rsync.

Automating it with a systemd timer (shipped in this repo under `deploy/systemd/`):

```bash
sudo cp deploy/systemd/spirrow-conclair-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spirrow-conclair-backup.timer
systemctl list-timers spirrow-conclair-backup.timer
```

It fires daily at 04:30 JST, and `Persistent=true` means schedules missed while the host was down are caught up.

### Moving to rsync once the NAS is connected

When an export path on the NAS is available (e.g. `/mnt/nas/backups/spirrow-conclair/`):

```bash
# Add to ExecStartPost in /etc/systemd/system/spirrow-conclair-backup.service
ExecStartPost=/usr/bin/rsync -a --delete {{PATH_SERVICES}}/spirrow-conclair/backups/ /mnt/nas/backups/spirrow-conclair/

# Or switch BACKUP_DIR=/mnt/nas/... inside backup.sh
```

`--delete` makes the NAS drop the old snapshots that local `RETENTION_DAYS=30` has already removed. If the NAS is meant for long-term retention, drop `--delete` and manage retention there separately.

Restoring (prompts for confirmation, stops the conclair service while it runs):

```bash
sudo ./scripts/restore.sh backups/conclair-20260501T021129Z.dump.gz
```

## Troubleshooting

### conclair will not start

```bash
sudo systemctl status spirrow-conclair.service
sudo journalctl -u spirrow-conclair.service -n 100 --no-pager
```

Common causes:
- infra-stack is not running → `sudo systemctl start infra-stack.service`
- `DATABASE_URL` in `.env` is wrong → check it against `CONCLAIR_APP_PASSWORD` in `{{PATH_SERVICES_ROOT}}/infra/.env`
- an alembic migration failed → run `.venv/bin/alembic upgrade head` by hand

### Reaching the database directly

```bash
docker exec -it infra-postgres psql -U conclair_app -d conclair
# If it asks for a password: PGPASSWORD=$(grep CONCLAIR_APP_PASSWORD {{PATH_SERVICES_ROOT}}/infra/.env | cut -d= -f2)
```

### Logs for infra-postgres / infra-redis

```bash
docker logs infra-postgres --tail 100
docker logs infra-redis --tail 100
```

## Progress

Tasks are tracked in the magickit project (`spirrow-conclair`) in `spirrow-magickit`.

- `design` (T02) — OpenAPI / status / error envelope
- `implementation` (T03-T08) — scaffolding / models / services / api endpoints
- `testing` (T09-T10) — unit + integration (testcontainers postgres)
- `deployment` (T11-T14) — infra-stack / systemd / docs / backup timer
- **UI** (T15-T18) — Jinja2 + HTMX + plain CSS, `/ui` mount, open / post / close forms

154 / 154 tests pass, coverage 78%.
