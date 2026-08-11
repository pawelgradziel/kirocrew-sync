# KiroCrew App Implementation Plan

## Phase 1: Foundation (Backend Structure)

### 1.1 App Directory Structure

```bash
mkdir -p ~/.kiro/crew/apps/kirocrew-sync/{backend,data,ui/components}
cd ~/.kiro/crew/apps/kirocrew-sync
```

### 1.2 App Manifest (`app.json`)

```json
{
  "name": "kirocrew-sync",
  "version": "1.0.0",
  "displayName": "Sync",
  "description": "Synchronize KiroCrew data across machines with three-way merge. Keep knowledge, artifacts, and lessons consistent across your laptops and desktops, or share a knowledge library with your team.",
  "author": "kirocrew-sync",
  "tags": [
    "sync",
    "backup",
    "knowledge",
    "collaboration",
    "multi-machine"
  ],
  "highlights": [
    "Three-way merge: real bidirectional sync, per row, not per file",
    "Background daemon with adaptive intervals (30s when active, 5min idle)",
    "Dashboard page showing sync status, history, conflicts, and quarantine",
    "Notification channels for conflicts, failures, and quarantine",
    "Multiple backends: Google Drive, AWS S3, rsync, or local directory",
    "Team scope for knowledge sharing without personal data"
  ],
  "defaultEnabled": false,
  "iconUrl": "/app-assets/kirocrew-sync/icon.svg",
  "permissions": {
    "api": [
      "/api/apps/kirocrew-sync",
      "/api/apps/kirocrew-sync/*"
    ],
    "events": [
      "notification"
    ],
    "mcpTools": [
      "send_notification"
    ],
    "storage": true,
    "network": true,
    "cron": true
  },
  "ui": {
    "pages": [
      {
        "route": "/kirocrew-sync",
        "label": "Sync",
        "icon": "RefreshCw"
      }
    ]
  },
  "backend": {
    "entryPoint": "backend.server:app"
  },
  "crons": [
    {
      "name": "sync-daemon",
      "every": 300,
      "message": "Run sync via backend/sync_manager.py, store results in data/history.db, send notifications on conflicts/quarantine/failure. Skip if no changes detected (silent by design).",
      "persistent_session": false,
      "silent": true,
      "enabled": true
    }
  ],
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
  },
  "dependencies": {
    "commands": [
      "git"
    ],
    "optionalCommands": [
      "rclone",
      "aws",
      "rsync"
    ]
  }
}
```

### 1.3 Backend Structure

```
backend/
├── __init__.py
├── server.py           # FastAPI app entry point
├── routes.py           # API route handlers
├── sync_manager.py     # Wrapper around bash scripts
├── history.py          # Sync history tracking
├── conflicts.py        # Conflict management
├── backends.py         # Backend config/testing
├── database.py         # SQLite database operations
└── models.py           # Pydantic models
```

### 1.4 Database Schema (`data/history.db`)

```sql
CREATE TABLE sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    duration_ms INTEGER,
    scope TEXT NOT NULL,  -- 'personal' or 'team'
    strategy TEXT,
    dry_run BOOLEAN,
    changes_detected BOOLEAN,
    rows_merged INTEGER,
    conflicts_count INTEGER,
    quarantine_count INTEGER,
    output TEXT,
    error TEXT
);

CREATE TABLE sync_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    change_type TEXT NOT NULL,  -- 'knowledge', 'artifact', 'lesson', 'transcript'
    action TEXT NOT NULL,  -- 'added', 'updated', 'deleted', 'conflict'
    item_id TEXT,
    details TEXT,
    FOREIGN KEY (run_id) REFERENCES sync_runs(id)
);

CREATE TABLE conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    machine TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_id TEXT NOT NULL,
    local_value TEXT,
    remote_value TEXT,
    resolved BOOLEAN DEFAULT 0,
    resolution TEXT,  -- 'local-wins', 'remote-wins', 'manual'
    resolved_at TEXT,
    FOREIGN KEY (run_id) REFERENCES sync_runs(id)
);

CREATE TABLE quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine TEXT UNIQUE NOT NULL,
    reason TEXT NOT NULL,  -- 'version_mismatch', 'embedding_mismatch', 'scope_mismatch'
    detected_at TEXT NOT NULL,
    cleared_at TEXT,
    details TEXT
);

CREATE TABLE daemon_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

---

## Phase 2: Python Backend

### 2.1 Sync Manager (`backend/sync_manager.py`)

**Responsibilities**:
- Execute bash script with proper arguments
- Parse stdout/stderr
- Extract structured data (rows merged, conflicts, quarantine)
- Store results in history DB
- Return structured response

**Key methods**:
```python
class SyncManager:
    def run_sync(
        self,
        strategy: str = "auto",
        team: bool = False,
        dry_run: bool = False
    ) -> SyncResult
    
    def get_status(self) -> SyncStatus
    
    def check_running(self) -> bool
    
    def get_next_sync_time(self) -> datetime
```

### 2.2 History Manager (`backend/history.py`)

```python
class HistoryManager:
    def record_sync(self, result: SyncResult) -> int
    
    def get_recent(self, limit: int = 50) -> List[SyncRun]
    
    def get_run_details(self, run_id: int) -> SyncRunDetails
    
    def get_stats(self) -> SyncStats  # Total syncs, success rate, etc.
```

### 2.3 Conflict Manager (`backend/conflicts.py`)

```python
class ConflictManager:
    def get_unresolved(self) -> List[Conflict]
    
    def resolve_conflict(
        self,
        conflict_id: int,
        resolution: str  # 'local-wins', 'remote-wins'
    ) -> bool
    
    def mark_resolved(
        self,
        conflict_id: int,
        resolution: str,
        resolved_at: datetime
    )
```

### 2.4 Backend Config Manager (`backend/backends.py`)

```python
class BackendManager:
    def get_current_backend(self) -> BackendConfig
    
    def list_backends(self) -> List[BackendInfo]
    
    def test_connection(self, backend: str) -> TestResult
    
    def switch_backend(
        self,
        backend: str,
        config: Dict[str, Any]
    ) -> bool
    
    def update_config(
        self,
        backend: str,
        config: Dict[str, Any]
    ) -> bool
```

### 2.5 API Routes (`backend/routes.py`)

```python
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

# Status
@router.get("/status")
async def get_status() -> SyncStatusResponse:
    """Current sync state, last run, next run"""
    ...

# Manual sync
@router.post("/sync")
async def trigger_sync(
    strategy: str = "auto",
    team: bool = False,
    dry_run: bool = False
) -> SyncResultResponse:
    """Trigger manual sync"""
    ...

# History
@router.get("/history")
async def get_history(
    limit: int = 50,
    offset: int = 0
) -> HistoryResponse:
    """List recent syncs"""
    ...

@router.get("/history/{run_id}")
async def get_run_details(run_id: int) -> SyncRunDetails:
    """Detailed sync result with changes"""
    ...

# Conflicts
@router.get("/conflicts")
async def get_conflicts() -> ConflictsResponse:
    """Unresolved conflicts"""
    ...

@router.post("/conflicts/{conflict_id}/resolve")
async def resolve_conflict(
    conflict_id: int,
    resolution: str
) -> ConflictResolution:
    """Resolve conflict (local-wins/remote-wins)"""
    ...

# Quarantine
@router.get("/quarantine")
async def get_quarantine() -> QuarantineResponse:
    """Quarantined machines"""
    ...

@router.post("/quarantine/{machine}/force")
async def force_merge(machine: str) -> ForceResult:
    """Force merge quarantined machine"""
    ...

# Backends
@router.get("/backends")
async def list_backends() -> BackendsResponse:
    """Available backends and current config"""
    ...

@router.post("/backends/test")
async def test_backend(
    backend: str,
    config: Dict[str, Any]
) -> TestResult:
    """Test backend connection"""
    ...

@router.post("/backends/switch")
async def switch_backend(
    backend: str,
    config: Dict[str, Any]
) -> SwitchResult:
    """Switch to different backend"""
    ...

# Daemon control
@router.post("/daemon/control")
async def control_daemon(
    action: str  # 'start', 'stop', 'restart'
) -> DaemonControlResult:
    """Start/stop/restart daemon"""
    ...

@router.put("/daemon/config")
async def update_daemon_config(
    interval: int = None,
    scope: str = None
) -> DaemonConfig:
    """Update daemon config (interval, scope)"""
    ...
```

---

## Phase 3: Dashboard UI

### 3.1 Main Page Component

```typescript
// ui/components/SyncDashboard.tsx
import { StatusWidget } from './StatusWidget';
import { HistoryTimeline } from './HistoryTimeline';
import { ConflictPanel } from './ConflictPanel';
import { QuarantinePanel } from './QuarantinePanel';
import { DaemonControl } from './DaemonControl';
import { BackendConfig } from './BackendConfig';

export function SyncDashboard() {
  return (
    <div className="sync-dashboard">
      <div className="grid grid-cols-2 gap-4">
        <StatusWidget />
        <DaemonControl />
      </div>
      
      <Tabs>
        <TabPanel label="History">
          <HistoryTimeline />
        </TabPanel>
        <TabPanel label="Conflicts">
          <ConflictPanel />
        </TabPanel>
        <TabPanel label="Quarantine">
          <QuarantinePanel />
        </TabPanel>
        <TabPanel label="Backend">
          <BackendConfig />
        </TabPanel>
      </Tabs>
    </div>
  );
}
```

### 3.2 Status Widget

```typescript
// ui/components/StatusWidget.tsx
export function StatusWidget() {
  const { data: status } = useQuery('/api/apps/kirocrew-sync/status');
  
  return (
    <Card>
      <CardHeader>
        <StatusIndicator status={status.state} />
        <h3>Sync Status</h3>
      </CardHeader>
      <CardBody>
        <Stat label="Last sync" value={status.lastSync} />
        <Stat label="Next sync" value={status.nextSync} />
        <Stat label="Scope" value={status.scope} />
        <Stat label="Machines" value={`${status.machines} active`} />
      </CardBody>
      <CardFooter>
        <Button onClick={triggerSync}>Sync Now</Button>
      </CardFooter>
    </Card>
  );
}
```

### 3.3 History Timeline

```typescript
// ui/components/HistoryTimeline.tsx
export function HistoryTimeline() {
  const { data: history } = useQuery('/api/apps/kirocrew-sync/history');
  
  return (
    <Timeline>
      {history.runs.map(run => (
        <TimelineItem key={run.id}>
          <TimelineMarker status={run.exitCode === 0 ? 'success' : 'error'} />
          <TimelineContent>
            <Time>{run.timestamp}</Time>
            <Summary>{run.summary}</Summary>
            {run.changes && (
              <ChangesList>
                {run.changes.map(change => (
                  <ChangeItem key={change.id}>{change.description}</ChangeItem>
                ))}
              </ChangesList>
            )}
          </TimelineContent>
        </TimelineItem>
      ))}
    </Timeline>
  );
}
```

### 3.4 Conflict Panel

```typescript
// ui/components/ConflictPanel.tsx
export function ConflictPanel() {
  const { data: conflicts } = useQuery('/api/apps/kirocrew-sync/conflicts');
  const resolveConflict = useMutation('/api/apps/kirocrew-sync/conflicts/:id/resolve');
  
  return (
    <Table>
      <TableHeader>
        <th>Machine</th>
        <th>Item</th>
        <th>Type</th>
        <th>Actions</th>
      </TableHeader>
      <TableBody>
        {conflicts.map(conflict => (
          <TableRow key={conflict.id}>
            <td>{conflict.machine}</td>
            <td>{conflict.item}</td>
            <td>{conflict.type}</td>
            <td>
              <Button onClick={() => resolveConflict(conflict.id, 'local-wins')}>
                Keep Local
              </Button>
              <Button onClick={() => resolveConflict(conflict.id, 'remote-wins')}>
                Keep Remote
              </Button>
              <Button onClick={() => showDiff(conflict)}>
                View Diff
              </Button>
            </td>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
```

### 3.5 Daemon Control

```typescript
// ui/components/DaemonControl.tsx
export function DaemonControl() {
  const { data: config } = useQuery('/api/apps/kirocrew-sync/daemon/config');
  const controlDaemon = useMutation('/api/apps/kirocrew-sync/daemon/control');
  const updateConfig = useMutation('/api/apps/kirocrew-sync/daemon/config');
  
  return (
    <Card>
      <CardHeader>
        <h3>Daemon Control</h3>
      </CardHeader>
      <CardBody>
        <Toggle
          checked={config.enabled}
          onChange={enabled => controlDaemon(enabled ? 'start' : 'stop')}
          label="Sync daemon"
        />
        
        <Select
          value={config.scope}
          onChange={scope => updateConfig({ scope })}
          options={['personal', 'team']}
          label="Scope"
        />
        
        <Slider
          value={config.interval}
          onChange={interval => updateConfig({ interval })}
          min={60}
          max={900}
          step={60}
          label="Interval (seconds)"
        />
      </CardBody>
    </Card>
  );
}
```

### 3.6 Backend Configuration

```typescript
// ui/components/BackendConfig.tsx
export function BackendConfig() {
  const { data: backends } = useQuery('/api/apps/kirocrew-sync/backends');
  const testBackend = useMutation('/api/apps/kirocrew-sync/backends/test');
  const switchBackend = useMutation('/api/apps/kirocrew-sync/backends/switch');
  
  return (
    <div className="backend-grid">
      {backends.available.map(backend => (
        <BackendCard
          key={backend.name}
          backend={backend}
          active={backend.name === backends.current}
          onTest={() => testBackend(backend.name)}
          onSwitch={() => switchBackend(backend.name)}
        />
      ))}
    </div>
  );
}
```

---

## Phase 4: Notifications

### 4.1 Notification Service

```python
# backend/notifications.py
from kiro_crew.mcp import send_notification

class NotificationService:
    async def notify_sync_completed(
        self,
        changes: List[str],
        conflicts: int,
        quarantine: int
    ):
        """Send notification only if actionable."""
        if conflicts > 0 or quarantine > 0:
            await send_notification(
                channel="sync-completed",
                title="Sync completed with issues",
                body=f"{conflicts} conflicts, {quarantine} quarantined",
                actions=[{
                    "label": "View conflicts",
                    "url": "/apps/kirocrew-sync?tab=conflicts"
                }]
            )
    
    async def notify_conflict(self, conflict: Conflict):
        await send_notification(
            channel="conflict-detected",
            title=f"Conflict on {conflict.machine}",
            body=f"{conflict.table}.{conflict.row_id}",
            actions=[{
                "label": "Resolve",
                "url": f"/apps/kirocrew-sync?tab=conflicts&id={conflict.id}"
            }]
        )
    
    async def notify_quarantine(self, machine: str, reason: str):
        await send_notification(
            channel="quarantine",
            title=f"Machine quarantined: {machine}",
            body=reason,
            actions=[{
                "label": "View details",
                "url": "/apps/kirocrew-sync?tab=quarantine"
            }]
        )
    
    async def notify_failure(self, error: str):
        await send_notification(
            channel="sync-failed",
            title="Sync failed",
            body=error,
            actions=[{
                "label": "View logs",
                "url": "/apps/kirocrew-sync?tab=history"
            }]
        )
```

---

## Phase 5: Integration & Testing

### 5.1 Install Script

```bash
#!/usr/bin/env bash
# install-kirocrew-app.sh

APP_DIR="$HOME/.kiro/crew/apps/kirocrew-sync"
SYNC_DIR="$HOME/.kiro/crew/workspace/kirocrew-sync"

# Check if sync engine is installed
if [ ! -d "$SYNC_DIR" ]; then
    echo "❌ kirocrew-sync not found at $SYNC_DIR"
    echo "Install from: https://github.com/pawelgradziel/kirocrew-sync"
    exit 1
fi

# Create app directory
mkdir -p "$APP_DIR"/{backend,data,ui/components}

# Copy files
cp app.json "$APP_DIR/"
cp -r backend/* "$APP_DIR/backend/"
cp -r ui/* "$APP_DIR/ui/"

# Initialize database
python3 "$APP_DIR/backend/database.py" init

# Create installed.json
echo '{"installedAt": "'$(date -Iseconds)'", "version": "1.0.0"}' > "$APP_DIR/installed.json"

echo "✅ KiroCrew Sync app installed"
echo "Enable it in Settings → Apps → kirocrew-sync"
```

### 5.2 Test Plan

**Unit tests**:
- `test_sync_manager.py` - Bash wrapper parsing
- `test_history.py` - Database operations
- `test_conflicts.py` - Conflict resolution
- `test_backends.py` - Backend config/testing

**Integration tests**:
- End-to-end sync flow
- Conflict resolution workflow
- Notification delivery
- Daemon control

**Manual testing checklist**:
- [ ] Install app from Settings → Apps
- [ ] Dashboard page loads at `/apps/kirocrew-sync`
- [ ] Status widget shows current state
- [ ] Manual "Sync Now" button works
- [ ] History timeline displays recent syncs
- [ ] Conflict resolution buttons work
- [ ] Quarantine panel shows incompatible machines
- [ ] Daemon toggle starts/stops cron
- [ ] Scope selector switches personal/team
- [ ] Backend test connection works
- [ ] Backend switching works
- [ ] Notifications arrive for conflicts
- [ ] Notifications arrive for quarantine
- [ ] Notifications arrive for failures

---

## Timeline Estimate

**Phase 1 (Foundation)**: 2-3 hours
- App manifest
- Database schema
- Directory structure

**Phase 2 (Backend)**: 1-2 days
- Sync manager wrapper
- History/conflicts/backends managers
- API routes
- Notification service

**Phase 3 (UI)**: 1-2 days
- Dashboard page layout
- Status widget
- History timeline
- Conflict/quarantine panels
- Daemon control
- Backend config

**Phase 4 (Polish)**: 4-6 hours
- Error handling
- Loading states
- Empty states
- Theme support (light/dark)

**Phase 5 (Testing)**: 4-6 hours
- Unit tests
- Integration tests
- Manual testing

**Total**: 3-4 days for MVP

---

## Open Questions

1. **Icon assets**: Where to source SVG icons? Use existing KiroCrew icon set?
2. **Hero images**: Need light/dark variants for app marketplace
3. **Permissions**: Do we need more granular MCP tool access?
4. **Cron scheduling**: 5 minutes reasonable default? Make configurable?
5. **History retention**: Keep last 50/100/500 syncs? Prune strategy?
6. **Real-time updates**: WebSocket for live sync status, or polling?

---

## Next Steps

1. Create `app.json` manifest
2. Set up database schema
3. Build `SyncManager` wrapper
4. Implement basic API routes
5. Create status widget UI
6. Wire up notifications
7. Test end-to-end flow
