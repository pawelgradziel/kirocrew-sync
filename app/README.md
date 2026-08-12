# KiroCrew Sync App

Full KiroCrew application for managing sync across machines.

## Installation

```bash
./install-app.sh
```

Requires `kirocrew-sync` to already be installed at
`~/.kiro/crew/workspace/kirocrew-sync` and `python3` on `PATH` — the script
checks both up front and aborts with a clear error (without copying anything)
if either is missing.

This will:
1. Copy `app.json`, the whole `backend/` directory, the whole `ui/` directory
   (components + the `icon.svg` asset), and `requirements.txt` to
   `~/.kiro/crew/apps/kirocrew-sync/`
2. Initialize the history database (safe to re-run — schema uses
   `CREATE TABLE IF NOT EXISTS` / `INSERT OR IGNORE`, so it never wipes
   existing history)
3. Write/refresh `installed.json`

The script is safe to re-run any time to pick up a newer checkout: it
overwrites app files but preserves sync history and the original
`installedAt` timestamp (a separate `updatedAt` field tracks the latest run).

## Uninstalling

```bash
./install-app.sh --uninstall
```

Removes `~/.kiro/crew/apps/kirocrew-sync`, including its history database.
It does **not** touch your `kirocrew-sync` checkout at
`~/.kiro/crew/workspace/kirocrew-sync`. Safe to run even if nothing is
installed. After uninstalling, also disable/remove the app in
**Settings → Apps** and restart the gateway.

## Enabling

1. Restart KiroCrew gateway (if running)
2. Go to **Settings → Apps → kirocrew-sync**
3. Click **Enable**
4. Navigate to **/apps/kirocrew-sync** in dashboard

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
│   └── components/
│       ├── SyncDashboard.tsx    # Page shell: status + daemon control + tabs
│       ├── StatusWidget.tsx
│       ├── DaemonControl.tsx
│       ├── HistoryTimeline.tsx
│       ├── ConflictPanel.tsx
│       ├── QuarantinePanel.tsx
│       └── BackendConfig.tsx
└── data/
    └── history.db                # Sync history database
```

## Database Schema

**sync_runs**: Sync run records with timestamps, exit codes, changes
**sync_changes**: Individual changes per run (knowledge, artifacts, etc.)
**conflicts**: Unresolved conflicts with local/remote values
**quarantine**: Quarantined machines with reasons
**daemon_state**: Daemon configuration (enabled, scope, interval)

## Development

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
