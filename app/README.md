# KiroCrew Sync App

Full KiroCrew application for managing sync across machines.

## Installation

```bash
./install-app.sh
```

This will:
1. Copy app files to `~/.kiro/crew/apps/kirocrew-sync/`
2. Initialize the history database
3. Create installation metadata

## Enabling

1. Restart KiroCrew gateway (if running)
2. Go to **Settings → Apps → kirocrew-sync**
3. Click **Enable**
4. Navigate to **/apps/kirocrew-sync** in dashboard

## Features

### Dashboard Page

**Status Widget**:
- Last sync time
- Next sync time
- Current state (idle/syncing/conflict/failed/quarantine)
- Machine counts

**History Timeline**:
- Last 50 syncs with timestamps
- What changed (rows merged, conflicts, quarantine)
- Detailed change view on expand

**Conflict Panel**:
- Unresolved conflicts with diffs
- Resolve buttons (Keep Local / Keep Remote)
- Bulk resolution

**Quarantine Panel**:
- Which machines are quarantined and why
- Clear quarantine button

**Daemon Control**:
- Pause/Resume toggle
- Scope selector (Personal / Team)
- Interval slider

**Backend Configuration**:
- List of available backends
- Test connection button
- Switch backend form

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

All at `/api/apps/kirocrew-sync/*`:

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
| `/daemon/config` | GET/PUT | Daemon configuration |
| `/daemon/control` | POST | Start/stop/restart |

## Architecture

```
~/.kiro/crew/apps/kirocrew-sync/
├── app.json                    # App manifest
├── installed.json              # Installation metadata
├── backend/
│   ├── __init__.py
│   ├── server.py              # FastAPI app
│   ├── database.py            # SQLite schema
│   ├── models.py              # Pydantic models
│   ├── sync_manager.py        # Bash script wrapper
│   ├── history.py             # Sync history tracking
│   ├── conflicts.py           # Conflict management
│   ├── quarantine.py          # Quarantine tracking
│   └── backends.py            # Backend config/testing
└── data/
    └── history.db             # Sync history database
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
