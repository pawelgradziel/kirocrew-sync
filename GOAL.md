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
- ✅ Phase 2: Backend complete (all managers, FastAPI server, 15 routes)
- 🔄 **Next**: Phase 3 - UI components or test backend
- ⏳ Then: Phase 4 - Polish and error handling
- ⏳ Then: Phase 5 - End-to-end testing

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
