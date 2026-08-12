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

All at `/api/apps/kirocrew-sync/*` (relative paths below are what
`backend/server.py` defines; the gateway mounts them under that prefix per
`app.json`'s `permissions.api`):

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
│   ├── history.py               # Sync history persistence
│   ├── conflicts.py             # Conflict persistence/resolution
│   ├── quarantine.py            # Quarantine persistence
│   ├── backends.py              # Backend config/testing
│   ├── artifacts.py             # Parses conflicts/quarantine logs written by the bash sync engine
│   └── notifications.py         # Delivers notifications to the KiroCrew gateway
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
    └── history.db                # Sync history database
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
