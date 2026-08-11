# KiroCrew App for kirocrew-sync - Summary

## Current State (What We Have)

### Core Sync Engine ✅
- Three-way merge with per-row conflict resolution
- Multiple storage backends (Google Drive, S3, rsync, local)
- Knowledge path portability across machines
- Team scope for knowledge sharing
- Quarantine for incompatible machines
- Exit codes: 0 (success), 1 (stopped), 3 (partial/quarantine)

### Current Integration (Master Branch)
1. **Bash daemon** (`lib/daemon.sh`)
   - Adaptive intervals: 30s active, 5min idle, 10min backoff
   - Smart change detection without full sync
   - Activity-aware (skips when KiroCrew is writing)
   - Cross-platform (Linux/macOS)

2. **System services**
   - `contrib/systemd/kirocrew-sync.service` (Linux)
   - `contrib/launchd/com.kirocrew.sync.plist` (macOS)

3. **KiroCrew Skill** (`contrib/kirocrew-skill/SKILL.md`)
   - Natural language triggers: "sync my data", "check sync status"
   - Command documentation
   - Troubleshooting guide

4. **KiroCrew Cron** (`contrib/kirocrew-cron/sync_daemon.py`)
   - Script-based (zero tokens)
   - Parses sync output for notifications
   - Respects team scope config

### Current UX Problems ❌

**No visibility**:
- Can't see sync status from dashboard
- No indication when last sync ran
- No way to know if sync is running now
- History only in git log

**Manual management**:
- Must run `kirocrew cron add` with complex config
- Start/stop requires systemd/launchctl commands
- Pause/resume needs job ID lookup
- Scope switching requires editing service files

**Poor error handling**:
- Conflicts logged to `~/.kiro/crew/.sync/conflicts.jsonl`
- Quarantine status in `~/.kiro/crew/.sync/quarantine.txt`
- No notifications when sync fails
- User must manually check logs

**Hidden backend config**:
- Google Drive/S3/rsync configured in text files
- No way to test connection from UI
- Can't switch backends without editing config

---

## What We're Building: Full KiroCrew App

A first-class KiroCrew application that integrates sync into the dashboard with proper UI, notifications, and management.

### Architecture

```
~/.kiro/crew/apps/kirocrew-sync/
├── app.json                    # App manifest
├── installed.json              # Installation metadata
├── .app_secret                 # Auto-generated auth token
├── backend/
│   ├── __init__.py
│   ├── routes.py              # FastAPI routes
│   ├── sync_manager.py        # Wrapper around bash script
│   ├── history.py             # Sync history tracking
│   ├── conflicts.py           # Conflict management
│   └── backends.py            # Backend config/testing
├── data/
│   ├── history.db             # Sync history database
│   ├── config.json            # Backend configuration
│   └── state.json             # Current sync state
├── ui/
│   └── components/            # React components
│       ├── StatusWidget.tsx
│       ├── HistoryTimeline.tsx
│       ├── ConflictPanel.tsx
│       ├── QuarantinePanel.tsx
│       └── BackendConfig.tsx
└── skills/
    └── kirocrew-sync.md       # App-bundled skill
```

### Key Features

#### 1. Dashboard Page (`/apps/kirocrew-sync`)

**Status Overview Panel**:
- Last sync: "2 minutes ago"
- Next sync: "in 3 minutes"
- Status indicator: ● Syncing / ✓ Up to date / ⚠ Conflict / ✗ Failed
- Scope badge: Personal / Team
- Machines: "3 active, 1 quarantined"

**Sync History Timeline**:
- Last 50 syncs with timestamps
- What changed: "5 knowledge items, 2 artifacts, 3 lessons"
- Duration and exit code
- Click to expand: detailed row-level changes
- Filter by: success/conflict/failure

**Active Conflicts Panel**:
- Table: Machine | Item | Type | Actions
- Resolve buttons: Keep Local | Keep Remote | Manual
- Bulk resolve: "Resolve all as local-wins"
- Shows conflict log with diffs

**Quarantine Panel**:
- Which machines and why: "laptop-xyz: KiroCrew v1.2.0 (current: v1.3.0)"
- Auto-dismiss when resolved
- "Force merge anyway" button (dangerous)

**Daemon Control**:
- Toggle: ⏸ Pause / ▶ Resume / 🔄 Restart
- Scope selector: Personal / Team
- Interval slider: 1min - 15min
- Manual trigger: "Sync Now" button

**Backend Configuration**:
- Card for each backend: Google Drive | AWS S3 | Rsync | Local
- Active backend highlighted
- Configure button → modal with form
- Test Connection button (shows success/error)
- Switch backend with confirmation

#### 2. Background Crons (App-Managed)

Defined in `app.json`, visible in dashboard Jobs list:

```json
{
  "crons": [
    {
      "name": "kirocrew-sync-daemon",
      "every": 300,
      "message": "Run sync via Python backend, store results in history.db, send notifications on conflicts/quarantine/failure",
      "persistent_session": false,
      "silent": true,
      "enabled": true
    }
  ]
}
```

User can pause/resume from:
- Jobs list in dashboard
- App's daemon control panel
- Automatically paused when "Pause daemon" toggled

#### 3. Notification Channels

```json
{
  "notifications": {
    "channels": [
      {
        "id": "sync-completed",
        "name": "Sync completed",
        "icon": "CheckCircle",
        "defaultPriority": "passive"
      },
      {
        "id": "conflict-detected",
        "name": "Conflict detected",
        "icon": "AlertTriangle",
        "defaultPriority": "medium"
      },
      {
        "id": "quarantine",
        "name": "Machine quarantined",
        "icon": "ShieldAlert",
        "defaultPriority": "critical"
      },
      {
        "id": "sync-failed",
        "name": "Sync failed",
        "icon": "XCircle",
        "defaultPriority": "critical"
      }
    ]
  }
}
```

Notifications only sent when actionable:
- "Sync completed" → only if conflicts or quarantine
- "Conflict detected" → needs user resolution
- "Quarantine" → machine needs attention
- "Sync failed" → backend unreachable or error

#### 4. API Routes

All at `/api/apps/kirocrew-sync/*`:

| Route | Method | Purpose |
|-------|--------|---------|
| `/status` | GET | Current sync state, last run, next run |
| `/sync` | POST | Trigger manual sync |
| `/history` | GET | Last 50 syncs with pagination |
| `/history/:id` | GET | Detailed sync result with changes |
| `/conflicts` | GET | Unresolved conflicts |
| `/conflicts/:id/resolve` | POST | Resolve conflict (local-wins/remote-wins) |
| `/quarantine` | GET | Quarantined machines |
| `/quarantine/:machine/force` | POST | Force merge quarantined machine |
| `/backends` | GET | Available backends and current config |
| `/backends/test` | POST | Test backend connection |
| `/backends/switch` | POST | Switch to different backend |
| `/daemon/control` | POST | Start/stop/restart daemon |
| `/daemon/config` | PUT | Update daemon config (interval, scope) |

#### 5. Integration with Existing Bash Scripts

The Python backend wraps the existing bash implementation:

```python
import subprocess
import json
from pathlib import Path

class SyncManager:
    def __init__(self):
        self.sync_dir = Path.home() / ".kiro/crew/workspace/kirocrew-sync"
        self.script = self.sync_dir / "kirocrew-sync.sh"
    
    def run_sync(self, strategy="auto", team=False, dry_run=False):
        """Run sync and parse structured output."""
        cmd = [str(self.script), "sync", "--strategy", strategy]
        if team:
            cmd.append("--team")
        if dry_run:
            cmd.append("--dry-run")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(self.sync_dir)
        )
        
        return self._parse_output(result)
```

**No rewrite needed** — bash scripts stay as the engine, Python just adds observability.

---

## Benefits Over Current Solution

| Aspect | Current | Full App |
|--------|---------|----------|
| **Visibility** | CLI/logs only | Dashboard page |
| **Status** | Must check manually | Always visible |
| **History** | Git log | Timeline UI |
| **Conflicts** | Text files | UI with resolve buttons |
| **Errors** | Logs | Notifications |
| **Daemon control** | systemd/CLI | Toggle button |
| **Backend config** | Edit files | UI form with test |
| **Scope switching** | Edit service file | Dropdown |
| **Quarantine** | Text file | Dashboard panel |
| **Multi-machine view** | None | Shows all machines |

---

## Success Criteria

The app is successful when a user can:

1. **See sync status at a glance** → Open dashboard, see "Synced 2m ago, next in 3m"
2. **Resolve conflicts visually** → Click conflict, see diff, click "Keep Local"
3. **Get notified on issues** → Notification appears when conflict or quarantine
4. **Control daemon easily** → Toggle pause/resume, no CLI needed
5. **Configure backends in UI** → Form with test button, no file editing
6. **View sync history** → Timeline showing what changed when
7. **Understand quarantine** → Panel explains why machine is quarantined

---

## Next Steps

See `docs/kirocrew-app-plan.md` for detailed implementation plan.
