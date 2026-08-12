# Goal: Build KiroCrew App for kirocrew-sync

## Primary Objective

Transform kirocrew-sync from a CLI tool with bolt-on integrations into a **first-class KiroCrew application** with proper dashboard UI, notifications, and management.

## Why

Current solution has critical UX gaps:
- ❌ No sync status visibility (must check logs)
- ❌ Manual cron management (complex JSON config)
- ❌ Conflicts hidden in text files
- ❌ No notifications on failures
- ❌ Backend configuration requires file editing

## What Success Looks Like

A user can:
1. Open dashboard → see "Synced 2m ago, next in 3m" status
2. Click conflict → see diff → click "Keep Local" → resolved
3. Get notification when conflict or quarantine happens
4. Toggle "Pause sync" button → daemon stops (no CLI)
5. Switch backend via UI form with "Test Connection" button
6. View timeline of last 50 syncs with what changed

## Approach

**Keep bash engine, add Python wrapper:**
- Existing bash scripts (`kirocrew-sync.sh`, `lib/daemon.sh`) remain
- Python backend (`backend/sync_manager.py`) calls bash, parses output
- Store results in `data/history.db` for dashboard queries
- FastAPI routes at `/api/apps/kirocrew-sync/*`
- React UI components for dashboard page

## Deliverables

1. **App Structure**
   - `~/.kiro/crew/apps/kirocrew-sync/app.json` (manifest)
   - `backend/` (Python FastAPI wrapper)
   - `data/` (history database, config)
   - `ui/components/` (React dashboard components)

2. **Dashboard Page** (`/apps/kirocrew-sync`)
   - Status widget (last sync, next sync, indicator)
   - History timeline (last 50 syncs)
   - Conflict resolution panel
   - Quarantine panel
   - Daemon control (pause/resume/restart)
   - Backend configuration form

3. **Background Crons**
   - Registered in `app.json`
   - Visible in dashboard Jobs list
   - Pause/resume from UI

4. **Notification Channels**
   - Sync completed (passive)
   - Conflict detected (medium)
   - Machine quarantined (critical)
   - Sync failed (critical)

5. **API Routes**
   - GET `/status` → current state
   - POST `/sync` → trigger manual sync
   - GET `/history` → sync timeline
   - POST `/conflicts/:id/resolve` → resolve conflict
   - GET `/quarantine` → quarantined machines
   - POST `/backends/test` → test backend connection
   - POST `/daemon/control` → start/stop/restart

## Non-Goals

- ❌ Rewriting sync engine in Python (bash stays)
- ❌ Real-time sync (polling is fine)
- ❌ Mobile app
- ❌ Multi-user permissions (KiroCrew apps are single-user)

## Success Metrics

- User never has to edit config files manually
- User never has to check logs for sync status
- User can resolve conflicts in < 10 seconds
- User gets notified when action needed
- Backend switching takes < 5 clicks

## Current Status

- ✅ Research completed (KiroCrew App platform understood)
- ✅ Summary document created
- ✅ Implementation plan created
- ✅ Phase 1: Foundation complete (app.json, database schema, models)
- ✅ Phase 2: Backend complete (all managers, FastAPI server, 20 routes)
- ✅ Phase 3: UI complete (6 dashboard components incl. BackendConfig)
- ✅ Phase 4: Notifications, error handling, loading/empty/error states
- ✅ Phase 5: 180 unit tests passing, hermetic (never touches ~/.kiro/crew)

**Verified against the real KiroCrew host source** (`/home/pawel/code/kirocrew`):
`app.json` passes the host's own `AppManifest.validate()`, and the notification
transport uses the routes the gateway actually registers.

### Known limitations — read before calling this done

These are real gaps, deliberately recorded rather than hidden:

1. **Daemon interval/scope config is decorative.** `lib/daemon.sh` hardcodes
   `INTERVAL_IDLE/ACTIVE/BACKOFF` and never reads the app's `daemon_state`
   table, so `PUT /daemon/config {"interval": N}` does not change what the
   running daemon does. `next_sync` now returns `null` rather than fabricating
   a schedule the daemon does not follow.
2. **Syncs run by the bash daemon are invisible to the app.** Artifact
   ingestion happens only in `POST /sync`. A daemon-only user will see empty
   `/conflicts` and `/quarantine`, because each daemon run truncates and
   rewrites those logs before the app ever reads them. The app.json cron
   (backend-driven) is the path that works today.
3. **`sync_changes` is never populated.** `GET /history/:id` always returns
   `changes: []`. The table, model and CHECK constraints exist but nothing
   writes to them, so "what changed" per run is not available yet.
4. **A SIGKILLed daemon can orphan a running sync.** `kirocrew-sync.sh` has no
   lock of its own, so a sync it spawned survives. The API says so rather than
   claiming a clean stop.
5. **UI is not compile-verified.** This repo ships no build tooling for the
   React components (`@/components/ui/*` resolves inside the host app), so the
   .tsx files have never been type-checked or rendered.
6. **Never run end-to-end against a real remote backend.** gdrive/s3/rsync
   paths are exercised only against stubs and a local directory.
7. **`iconPath` only resolves if published to a registry.** A side-loaded
   install has no route serving an app's own `ui/` dir; the lucide `icon`
   name is what actually renders in the nav.

## What's Built So Far

**Foundation** (Phase 1):
- `app/app.json` - Complete manifest with crons, notifications, permissions
- `app/backend/database.py` - SQLite schema (5 tables)
- `app/backend/models.py` - 20+ Pydantic models
- `app/backend/sync_manager.py` - Bash script wrapper

**Backend API** (Phase 2):
- `app/backend/history.py` - Sync history with pagination
- `app/backend/conflicts.py` - Conflict resolution
- `app/backend/quarantine.py` - Machine quarantine tracking
- `app/backend/backends.py` - Backend config & testing
- `app/backend/server.py` - FastAPI with 15 routes
- `install-app.sh` - Installation script
- `test_backend.py` - Backend validation tests

**UI** (Phase 3):
- `app/ui/components/SyncDashboard.tsx` - page layout, 4 tabs
- `app/ui/components/StatusWidget.tsx` - status, Sync Now
- `app/ui/components/DaemonControl.tsx` - start/stop/restart, scope, interval
- `app/ui/components/HistoryTimeline.tsx` - timeline, expand for run detail
- `app/ui/components/ConflictPanel.tsx` - resolve + local/remote diff
- `app/ui/components/QuarantinePanel.tsx` - quarantined machines
- `app/ui/components/BackendConfig.tsx` - cards, test, switch, configure form
- `app/ui/assets/icon.svg` - theme-agnostic stroke mark

**Phase 4/5 additions**:
- `app/backend/notifications.py` - 4 channels, dedup, degrades without transport
- `app/backend/artifacts.py` - parses conflicts.jsonl / quarantine.txt
- `app/tests/` - 180 tests

**Routes Implemented**:
- GET /status - current sync state
- POST /sync - trigger manual sync
- GET /history - sync timeline with pagination
- GET /history/:id - detailed run information
- GET /conflicts - unresolved conflicts
- POST /conflicts/:id/resolve - resolve conflict
- GET /quarantine - quarantined machines
- POST /quarantine/:machine/clear - clear quarantine
- GET /backends - available backends
- POST /backends/test - test connection
- POST /backends/switch - switch backend
- GET /backends/:name/config - effective settings for one backend
- PUT /backends/:name/config - persist settings (shell-quoted, injection-tested)
- GET /daemon/config - get daemon config
- PUT /daemon/config - update daemon config
- POST /daemon/control - start/stop/restart daemon

## Reference Documents

- `docs/kirocrew-app-summary.md` - What we're building and why
- `docs/kirocrew-app-plan.md` - Implementation plan (to be created)
- `docs/kirocrew-integration.md` - Current integration options
- `docs/daemon.md` - Current bash daemon docs

## Branch

`feature/kirocrew-app` - Keep this branch focused on the app only

## Keep in Mind

- Trust model: Apps run in-process with full gateway privileges
- Backend must respect existing config in `config.sh`
- History DB schema must support pagination and filtering
- UI must work in both light and dark themes
- Notifications should be actionable, not noisy
