# Crew Sync (KiroCrew app)

Full KiroCrew application for managing sync across machines.

## Installation

```bash
./install-app.sh
```

Requires `kirocrew-sync` to already be installed at
`~/.kiro/crew/workspace/kirocrew-sync` and `python3` on `PATH` — the script
checks both up front and aborts with a clear error (without copying anything)
if either is missing.

The script tries two paths, in order:

1. **Through the gateway (preferred).** If a KiroCrew gateway is reachable on
   `localhost` (`http://127.0.0.1:5476` by default; override with
   `KIROCREW_PORT`), the script mints a short-lived local token from
   `~/.kiro/crew/.local_secret` (the same loopback-only bootstrap KiroCrew's
   own local tooling uses — see `GET /api/token/local`) and calls the real
   `POST /api/apps/install` transaction, pointed at this repo's `app/`
   directory. This is the same thing the dashboard's **Apps → (sources icon)
   → Install from Path** does, and it's the only path that registers the
   app's agents/skills/crons, starts its backend, and generates its
   `.app_secret`. If the app is already installed this way, the script calls
   the update endpoint instead, so re-running is idempotent either way.

2. **Offline staging (fallback).** If no gateway is reachable, the script
   copies `app.json`, the whole `backend/` directory, the whole `ui/`
   directory, and `requirements.txt` to `~/.kiro/crew/apps/kirocrew-sync/`
   itself, initializes the history database schema, and writes a complete
   `installed.json` (with the app's real `name`, `origin: "local"`,
   `resources`/`lifecycle: "gateway"`) plus a freshly generated
   `.app_secret` — matching what the gateway's own installer would have
   produced. It does **not** register agents/skills/crons or start the
   backend; only a running gateway does that, the next time you enable the
   app from its dashboard.

Either way, once the app files are in place at their installed location the
script also provisions a dedicated backend virtualenv there
(`<installed app dir>/.venv`) and installs `requirements.txt`
(fastapi/uvicorn/pydantic) into it. This is required: the app's backend runs
as an ASGI process under `uvicorn`, and the gateway only uses its own bundled
interpreter as a fallback when no such venv is present — that interpreter
does **not** carry fastapi/uvicorn/pydantic, so without this step the backend
would fail to start as soon as it's enabled. If venv creation or the
dependency install fails (no network, no `python3-venv`), the script removes
whatever it managed to build rather than leaving a half-working venv behind,
and prints the exact commands to finish the setup by hand.

Either way, re-running the script is safe: it overwrites app files but
preserves sync history, the original `installedAt` timestamp, an
already-generated `.app_secret`, and an already-working backend venv (it's
only rebuilt if missing or missing a required package).

**Important:** installing (through either path) does not enable the app.
Third-party app execution is denied by default, so its backend and crons
will not run until you also **trust** it — see Enabling below.

## Uninstalling

```bash
./install-app.sh --uninstall
```

If a gateway is reachable, this calls `POST /api/apps/kirocrew-sync/uninstall`
so resources are properly deregistered and the backend is stopped before the
files are removed. Otherwise it falls back to removing
`~/.kiro/crew/apps/kirocrew-sync` directly (including its history database) —
in that case, if the app was ever enabled, its registered agents/skills/crons
are left stale until you disable it from the dashboard yourself. Either way it
does **not** touch your `kirocrew-sync` checkout at
`~/.kiro/crew/workspace/kirocrew-sync`, and it's safe to run even if nothing
is installed.

## Enabling

Required after every install, regardless of which install path ran, because
third-party app execution is denied by default:

1. **Settings → Security → Third-party apps** → trust `kirocrew-sync` (or
   turn on **Allow all third-party apps**). Until this app is trusted, the
   gateway's boot-time reconcile revokes its executable resources on every
   restart — its backend won't run and no notifications will be delivered,
   even if it shows as "enabled".
2. **Apps → Crew Sync** → click **Enable**. This is what actually registers its
   agents/skills/crons and starts its backend (the install step above never
   does this by itself, even through the gateway).
3. Navigate to **/apps/kirocrew-sync** in the dashboard.

## Features

### Dashboard Page

The page (`SyncDashboard`) shows a **Status Widget** and **Daemon Control**
card side by side, followed by four tabs: **History**, **Conflicts**,
**Quarantine**, and **Backend**.

**Status Widget** (always visible):
- State badge (Up to date / Syncing / Conflicts / Failed / Quarantine)
- Last sync / next sync time
- Scope (personal/team) and active/quarantined machine counts
- "Sync Now" button to trigger a manual sync

**Daemon Control** (always visible):
- "Background sync" switch — starts/stops the daemon (not just a config flag)
- "Restart daemon" button
- Scope selector (Personal / Team)
- Interval slider, 1–15 minutes

**History tab**:
- Last 50 syncs, each expandable to show individual changes (type, action,
  item id, details)
- "Showing 50 of N syncs" note when there is more history than that

**Conflicts tab**:
- Table of unresolved conflicts (machine / table / row)
- Per-row "View Diff" (local vs. remote value) and Keep Local / Keep Remote
  buttons

**Quarantine tab**:
- Quarantined machines with reason and details
- "Dismiss" button — quarantine clears automatically once versions match on
  the next successful sync; Dismiss only hides the record locally, it does
  not force a merge

**Backend tab**:
- A card per available backend (gdrive / s3 / rsync / local) showing
  configured/active state and required config keys
- "Test Connection" and "Switch" buttons per backend

### Background Cron

Registered in `app.json`, runs every 5 minutes:
- Executes sync via Python backend
- Stores results in history database
- Sends notifications on conflicts/quarantine/failure
- Silent when no changes detected

### Notification Channels

- **Sync completed** (passive) - only if conflicts or quarantine
- **Conflict detected** (medium) - needs resolution
- **Machine quarantined** (critical) - needs attention
- **Sync failed** (critical) - backend unreachable

### API Routes

Two different path shapes are involved, and they are NOT the same string:

- **Browser-facing (what the dashboard UI fetches):**
  `/apps/kirocrew-sync/api/<route>` — KiroCrew's gateway registers a
  same-origin reverse proxy at `/apps/{name}/api/{path:.*}`
  (`handle_app_api_proxy` in the gateway's `src/kiro_crew/apps/routes.py`)
  specifically for dashboard app UIs to call their own backend without CORS.
  This is the only path shape the UI is allowed to fetch — enforced both by
  the app-sdk's `createScopedApi` allowlist in the browser and by
  `app.json`'s `permissions.api`, which declares exactly this prefix.
- **Backend-facing (what `backend/server.py` defines and what curling the
  process directly hits):** `/api/<route>` — the proxy forwards a request
  for `/apps/kirocrew-sync/api/<route>` to this backend as
  `{backend_url}/api/<route>`, re-adding the `/api/` prefix it stripped off
  the incoming route. So every route below is declared on an
  `APIRouter(prefix="/api")` in `server.py`, `/health` excepted (see below).

The `<route>` column is relative — prefix it with `/apps/kirocrew-sync/api`
to get the real browser-facing URL, or with `/api` to get what
`backend/server.py` actually serves and what these routes are curl-able at
directly on the backend process:

| Route | Method | Purpose |
|-------|--------|---------|
| `/status` | GET | Current sync state |
| `/sync` | POST | Trigger manual sync |
| `/history` | GET | Last N syncs |
| `/history/:id` | GET | Detailed sync result |
| `/conflicts` | GET | Unresolved conflicts |
| `/conflicts/:id/resolve` | POST | Resolve conflict |
| `/quarantine` | GET | Quarantined machines |
| `/quarantine/:machine/clear` | POST | Clear quarantine |
| `/backends` | GET | Available backends |
| `/backends/test` | POST | Test backend connection |
| `/backends/switch` | POST | Switch backend |
| `/backends/:name/config` | GET | Effective settings for one backend |
| `/backends/:name/config` | PUT | Persist settings for one backend |
| `/daemon/config` | GET/PUT | Daemon configuration |
| `/daemon/control` | POST | Start/stop/restart |

This table was checked against the live route table (`backend.server.app.routes`),
not just read from the source: every application route the server registers is
listed above, and none are undocumented. FastAPI's own `/docs`, `/redoc` and
`/openapi.json` are also present but are framework-provided, not app routes.

`/health` is the one route deliberately NOT under `/api/` — it's a liveness
probe the gateway polls directly against the backend's port
(`_health_check_loop` in the gateway's `kiro_crew/apps/backend.py`), never
through the reverse proxy, using `app.json`'s `backend.healthCheck` (default
`/health`). Moving it under `/api/` without also changing `healthCheck`
would make every poll 404 and the app would never be marked healthy.

## Logging

The backend logs to two places at once:

- **stderr** — captured by the KiroCrew gateway from the backend process it
  spawns; this is what shows up in the gateway's own process logs.
- **`<data dir>/backend.log`** — a rotating file (2MB × 3 backups, so it can
  never grow without bound) at the same data directory `history.db` lives in
  (`~/.kiro/crew/apps/kirocrew-sync/data/backend.log` by default, or
  wherever `KIROCREW_DIR`/`KIROCREW_SYNC_DB` resolve it to — see
  `backend/logging_setup.py`). If that file can't be created (read-only
  data dir, missing parent, disk full), the backend falls back to
  stderr-only and keeps serving requests rather than failing to start.

What gets logged:

- **One line per HTTP request** — method, path, query string, response
  status, and duration in milliseconds. 2xx/3xx log at INFO, 4xx at
  WARNING, 5xx at ERROR, so `grep -i error backend.log` or `grep -i warning
  backend.log` surfaces exactly the requests worth looking at. This answers
  "did this request even arrive?" — previously the backend logged nothing
  at all about requests it received.
- **The real detail behind every 500** — route handlers convert internal
  failures into a generic `HTTPException(500, "Internal server error")` so
  the client response never leaks filesystem paths or sqlite internals, but
  the *log* always carries the real exception type, message, and traceback.
- **A startup block**, logged once when the process starts: the resolved
  data dir, the resolved sync-engine directory and whether
  `kirocrew-sync.sh` was actually found there, the effective log level, and
  the full list of registered routes. This alone should answer "is this the
  build I think it is, and can it see the sync engine" without any further
  digging.

**Log level** is configurable via the `KIROCREW_SYNC_LOG_LEVEL` environment
variable (`DEBUG`, `INFO`, `WARNING`, `ERROR`, ...; default `INFO`). An
unrecognized value falls back to `INFO` rather than preventing startup.

**Background syncs log to `<data dir>/cron.log`, not `backend.log`.** The cron
entry point (`backend/cli.py`, see Background sync below) runs as its own
short-lived process every 5 minutes while the backend server keeps running, and
two processes must not share one rotating file handler — whichever one rotates
renames the file out from under the other, which then keeps appending to an
orphaned inode until its own rotation, losing log segments. Same directory,
same format, same level variable; look there when a background tick did
something unexpected.

## Background sync

The `sync-daemon` cron declared in `app.json` runs `backend/cli.py` — a plain
`python3` entry point that runs a sync and records it exactly as
`POST /api/sync` does (history row, conflict/quarantine ingestion,
`daemon_state`, notifications), in-process and with no HTTP hop. Both entry
points call the same `sync_runner.run_sync_and_record()`, so a background tick
and a "Sync Now" click leave identical state behind.

It exists because a cron entry cannot authenticate to this app's own HTTP API:
`command` crons are vetted against command substitution (so they cannot mint a
gateway token) and `script` crons must live outside the app package. Having the
cron call `kirocrew-sync.sh` directly instead — the previous arrangement —
synced for real but recorded nothing, so the dashboard stayed empty until
someone clicked "Sync Now". See `docs/kirocrew-app-summary.md` → "Background
Crons" for the full rationale, and `backend/cli.py`'s module docstring for the
runtime constraints (absolute paths, per-app venv, timeout headroom).

Runnable by hand, which is the quickest way to see what a tick actually does:

```bash
~/.kiro/crew/apps/kirocrew-sync/.venv/bin/python3 ~/.kiro/crew/apps/kirocrew-sync/backend/cli.py --strategy auto
```

## Architecture

```
~/.kiro/crew/apps/kirocrew-sync/
├── app.json                    # App manifest
├── installed.json              # Installation metadata
├── requirements.txt            # Python dependencies
├── backend/
│   ├── __init__.py
│   ├── server.py                # FastAPI app (route definitions)
│   ├── database.py              # SQLite schema, init/reset
│   ├── models.py                # Pydantic request/response models
│   ├── sync_manager.py          # Wraps the kirocrew-sync bash engine
│   ├── sync_runner.py           # Run-a-sync-and-record-it path shared by the HTTP route and the cron CLI
│   ├── cli.py                   # HTTP-free cron entry point — see Background sync above
│   ├── history.py               # Sync history persistence
│   ├── conflicts.py             # Conflict persistence/resolution
│   ├── quarantine.py            # Quarantine persistence
│   ├── backends.py              # Backend config/testing
│   ├── artifacts.py             # Parses conflicts/quarantine logs written by the bash sync engine
│   ├── notifications.py         # Delivers notifications to the KiroCrew gateway
│   └── logging_setup.py         # Configures stderr + rotating backend.log — see Logging below
├── ui/
│   ├── assets/
│   │   └── icon.svg             # App icon (stroke-based sync mark, matches the RefreshCw lucide icon declared in app.json)
│   ├── dist/
│   │   └── index.mjs             # Built ESM bundle — what ui.entry in app.json points at. Committed; see "Building the UI bundle" below
│   ├── index.tsx                 # Bundle entry — re-exports SyncDashboard as the default export AppHost's lazy() loads
│   ├── lib/
│   │   └── time.ts               # Small local relative/absolute time helpers (date-fns isn't a host-shared module)
│   └── components/
│       ├── SyncDashboard.tsx    # Page shell: status + daemon control + tabs
│       ├── StatusWidget.tsx
│       ├── DaemonControl.tsx
│       ├── HistoryTimeline.tsx
│       ├── ConflictPanel.tsx
│       ├── QuarantinePanel.tsx
│       ├── BackendConfig.tsx
│       └── shared.tsx            # Small pieces shared across the panels above (inline message banner, loading/error/code-pill helpers)
└── data/
    ├── history.db                # Sync history database
    ├── backend.log                # Rotating backend request/error log — see Logging above
    └── cron.log                   # Rotating log for background (cron) syncs — see Logging above
```

`ui/package.json`, `ui/esbuild.config.mjs`, `ui/tsconfig.json` and `ui/types/` are build-time only (dev dependencies, editor types) — none of them are needed on the machine the app runs on, and `ui/node_modules/` is gitignored and never copied by `install-app.sh`.

## Database Schema

**sync_runs**: Sync run records with timestamps, exit codes, changes
**sync_changes**: Individual changes per run (knowledge, artifacts, etc.)
**conflicts**: Unresolved conflicts with local/remote values
**quarantine**: Quarantined machines with reasons
**daemon_state**: Daemon configuration (enabled, scope, interval)

## Development

### Building the UI bundle

The dashboard is a normal React component (`app/ui/components/SyncDashboard.tsx`),
but KiroCrew's `AppHost` loads third-party apps as a prebuilt ESM bundle, not
raw `.tsx` — see `ui.entry` in `app.json`. React, ReactDOM, the JSX runtime,
`lucide-react`, `@tanstack/react-query`, `@kirocrew/app-sdk` and `@kirocrew/ui`
are all provided by the host at runtime (its import map resolves them to its
own already-running instances), so the build marks every one of them
`external` rather than bundling a second copy — bundling any of them would
break React hooks.

```bash
cd app/ui
npm install     # dev-only deps: esbuild + type stubs, never shipped
npm run build   # writes app/ui/dist/index.mjs
```

`app/ui/dist/index.mjs` is committed to this repo — installs are a plain file
copy with no Node/npm required on the target machine, so the build output has
to already be there before `install-app.sh` runs. Re-run `npm run build`
after editing anything under `app/ui/components/` or `app/ui/lib/`, and
commit the updated `dist/index.mjs` alongside the source change.

### Testing API Locally

```bash
cd ~/.kiro/crew/apps/kirocrew-sync/backend
python3 -m uvicorn server:app --reload --port 8001
```

Then visit `http://localhost:8001/docs` for API documentation.

### Resetting Database

```bash
python3 ~/.kiro/crew/apps/kirocrew-sync/backend/database.py reset
```

## Troubleshooting

### App doesn't appear in Settings

- Restart KiroCrew gateway
- Check `~/.kiro/crew/apps/kirocrew-sync/app.json` exists
- Check logs: `~/.kiro/crew/gateway.log`

### A dashboard panel says "Failed to load ..."

The error state now shows the real reason under the heading (HTTP status +
server message, or a distinct network-error label if the request never got
a response at all — see the shared fetch helper in `ui/lib/api.ts`) and the
exact URL that was requested. Cross-reference that against
`~/.kiro/crew/apps/kirocrew-sync/data/backend.log` (see Logging above) to
see the matching server-side request line — if there is no matching line at
all, the request never reached this backend (stale bundle, proxy
misconfiguration, wrong port), which is itself the useful signal.

### Sync fails with "script not found"

Ensure `kirocrew-sync` is installed at:
```
~/.kiro/crew/workspace/kirocrew-sync/
```

### Database errors

Reset the database:
```bash
python3 ~/.kiro/crew/apps/kirocrew-sync/backend/database.py reset
```

### Cron not running

- Check Jobs list in dashboard
- Verify app is enabled
- Check cron is not paused
