"""
FastAPI server for KiroCrew Sync app.
"""

import logging
import time
from pathlib import Path
from typing import Optional, Tuple

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from .logging_setup import configure_logging
from .models import (
    SyncStatusResponse, HistoryResponse, ConflictsResponse,
    QuarantineResponse, BackendsResponse, SyncTriggerResponse,
    ConflictResolution, DaemonConfig, DaemonControl, BackendConfig,
    BackendTestResult, BackendTestRequest, SyncStatus, SyncTriggerRequest,
    BackendName, BackendConfigStatus, BackendConfigUpdate
)
from . import sync_runner
from .sync_manager import SyncManager, SyncEngineUnavailable
from .history import HistoryManager
from .conflicts import ConflictManager
from .quarantine import QuarantineManager
from .backends import BackendManager

# Configured before anything else below so that every log call that happens
# as a side effect of constructing the managers just below (Database.initialize()
# logs "Database initialized at %s", etc.) is already captured to both
# stderr and the rotating backend.log file, not just whatever ran after.
configure_logging()

logger = logging.getLogger(__name__)


def _resolve_db_path() -> Optional[Path]:
    """
    Resolve where the app's own SQLite database lives.

    Delegates to sync_runner.resolve_db_path so this process and the cron
    CLI cannot drift onto different databases -- if they did, cron runs
    would be recorded somewhere the dashboard never reads, and the timeline
    would look empty while syncs were plainly happening. Kept as a thin
    wrapper because tests and the startup block reference this name.
    """
    return sync_runner.resolve_db_path()


app = FastAPI(title="KiroCrew Sync API")


# ---------------------------------------------------------------------------
# Request logging middleware
# ---------------------------------------------------------------------------
#
# The whole point: right now a failed dashboard fetch just says "Failed to
# load status" with nothing server-side to correlate it against -- no way to
# tell whether the request even arrived. This logs exactly one line per
# request (method, path, query string, response status, duration in ms), so
# that question always has an answer in backend.log even if nobody was
# watching stderr live.
#
# 2xx/3xx log at INFO, 4xx at WARNING, 5xx at ERROR -- so a `grep -i error`
# or `grep -i warning` over backend.log surfaces exactly the requests worth
# looking at.
#
# Every route handler in this module already wraps its body in
# `except Exception -> _internal_error()`, which logs the real exception
# (via logger.exception, full traceback included) before converting it to a
# generic HTTPException(500, "Internal server error") -- so the *server log*
# carries the real detail while the *client response* never does, which is
# deliberate (see _internal_error's docstring). This middleware's own
# try/except below exists for the rarer case of an exception that escapes
# *without* going through that pattern (e.g. a bug in a future route, or a
# failure in ASGI plumbing itself) -- Starlette's ServerErrorMiddleware
# (which wraps every user middleware, including this one) still turns that
# into a generic 500 for the client either way, but without this it would
# reach the client without ever being logged at all.
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    query = f"?{request.url.query}" if request.url.query else ""
    request_line = f"{request.method} {request.url.path}{query}"

    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = (time.perf_counter() - start) * 1000
        logger.error(
            "%s -> unhandled %s: %s (%.1fms)",
            request_line, type(exc).__name__, exc, duration_ms,
            exc_info=True,
        )
        raise

    duration_ms = (time.perf_counter() - start) * 1000
    status = response.status_code
    log = logger.info
    if status >= 500:
        log = logger.error
    elif status >= 400:
        log = logger.warning
    log("%s -> %s (%.1fms)", request_line, status, duration_ms)

    return response


# ---------------------------------------------------------------------------
# /api prefix
# ---------------------------------------------------------------------------
#
# The gateway's dashboard reverse proxy (handle_app_api_proxy in KiroCrew's
# src/kiro_crew/apps/routes.py, registered at
# `/apps/{name}/api/{path:.*}`) forwards a browser request for
# `/apps/kirocrew-sync/api/<route>` to this backend as
# `{backend_url}/api/<route>` -- it deliberately re-adds the `/api/` prefix
# it stripped off the incoming route so "the backend sees its own
# `/api/...` routes without needing any path-rewriting middleware" (that
# gateway source's own comment). So every application route here MUST live
# under `/api/`, or the proxy's forwarded request 404s even though curling
# the same route at its bare path on this process works fine -- which is
# exactly the bug this router prefix fixes. `/health` is the one deliberate
# exception; see the note above `get_health` below for why.
router = APIRouter(prefix="/api")

# Initialize managers. This is *mostly* raise-free on a machine where the
# bash sync engine isn't installed: SyncManager/BackendManager resolve their
# paths lazily and only raise SyncEngineUnavailable from operations that
# actually need the script, which routes below catch and turn into a 503.
# It is NOT raise-free with respect to the *data* directory, though:
# Database.__init__ does `mkdir(parents=True)` and HistoryManager/etc. call
# initialize() (opens sqlite, runs the schema) right here at import time, so
# an unwritable/unreadable data directory (permissions, read-only fs, disk
# full) makes constructing these managers -- and therefore importing this
# module -- raise, not degrade. Making that path degrade gracefully would
# need every handler below to tolerate a manager that failed to initialize,
# which is a larger change than this comment fix; flagged here rather than
# silently done.
_db_path = _resolve_db_path()
sync_mgr = SyncManager(db_path=_db_path)
history_mgr = HistoryManager(db_path=_db_path)
conflict_mgr = ConflictManager(db_path=_db_path)
quarantine_mgr = QuarantineManager(db_path=_db_path)
backend_mgr = BackendManager()


def _internal_error(where: str, exc: Exception) -> HTTPException:
    """Log the real exception server-side (which may contain filesystem
    paths, sqlite internals, etc.) and hand the client back a message that
    reveals none of that."""
    logger.exception("%s failed", where)
    return HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
#
# KiroCrew's gateway polls this endpoint (backend.healthCheck, default
# "/health") in a background loop right after spawning the backend process,
# and only starts proxying /apps/kirocrew-sync/api/* traffic to us once it
# responds with a non-error status (see _health_check_loop /
# get_app_backend_port in the gateway's kiro_crew/apps/backend.py). It is
# polled repeatedly for the lifetime of the app, so the handler must stay
# trivially cheap and must NEVER touch the database, the sync engine, or any
# subprocess -- unlike /status (which does), this can't be allowed to block
# or fail because of something unrelated to "is the process alive".
#
# Deliberately registered on `app`, NOT `router`: _health_check_loop builds
# its URL as `http://127.0.0.1:{port}{manifest.backend.healthCheck}` and
# hits it DIRECTLY against this process's port -- it does not go through the
# gateway's reverse proxy at all, so it never gets the `/api/` prefix the
# proxy re-adds for every other route. app.json does not override
# `backend.healthCheck`, so the gateway polls the default `/health` at the
# root; moving this route under `/api/` would make every health check 404
# and the app would never be marked healthy (and the proxy would then have
# nothing to resolve a target against -- see the enablement gate in
# handle_app_api_proxy).

@app.get("/health")
async def get_health():
    """Liveness probe. Always returns 200 with a static body if the process
    is up enough to handle a request -- deliberately does not check the
    database, the sync engine, or any external command."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Sync orchestration
# ---------------------------------------------------------------------------
#
# The whole "run the engine, then record a sync_runs row, ingest
# conflicts.jsonl/quarantine.txt, stamp daemon_state, prune history, and send
# notifications" sequence used to live in this file, as private helpers of the
# POST /sync route below. It now lives in sync_runner.py, because the cron
# needs the identical sequence and cannot reach this route to get it (a cron
# `command` cannot authenticate to the app's own HTTP API -- see cli.py's
# module docstring). Keeping one implementation is the point: a second copy in
# the CLI would drift from this one silently, and the symptom would be a
# dashboard that disagrees with itself depending on which entry point ran the
# sync.


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

def _compute_status() -> SyncStatus:
    last_sync = history_mgr.get_last_sync()
    quarantine_count = quarantine_mgr.get_quarantine_count()
    conflict_count = conflict_mgr.get_conflict_count()
    daemon_config = sync_mgr.get_daemon_config()
    team = daemon_config["scope"] == "team"

    if sync_mgr.is_running():
        state = "syncing"
    elif conflict_count > 0:
        state = "conflict"
    elif quarantine_count > 0:
        state = "quarantine"
    elif last_sync and last_sync.exit_code not in (0, 3):
        # Per kirocrew-sync.sh's contract: 0 = ok, 3 = ok but one or more
        # machines stayed quarantined (already surfaced above via
        # quarantine_count, and cleared once they rejoin), 1 = actually
        # failed. Only a genuine non-(0, 3) exit code is "failed" -- this
        # mirrors _parse_output()/send_sync_notifications() in
        # sync_manager.py/sync_runner.py, which already treat (0, 3) as success.
        state = "failed"
    else:
        state = "idle"

    return SyncStatus(
        state=state,
        last_sync=last_sync.timestamp if last_sync else None,
        next_sync=sync_mgr.get_next_sync(),
        scope=daemon_config["scope"],
        machines_active=sync_mgr.get_active_machine_count(team=team),
        machines_quarantined=quarantine_count,
        conflicts_pending=conflict_count
    )


@router.get("/status", response_model=SyncStatusResponse)
async def get_status():
    """Get current sync status."""
    try:
        status = await run_in_threadpool(_compute_status)
        return SyncStatusResponse(status=status)
    except Exception as e:
        raise _internal_error("GET /status", e)


# ---------------------------------------------------------------------------
# /sync
# ---------------------------------------------------------------------------

@router.post("/sync", response_model=SyncTriggerResponse)
async def trigger_sync(body: Optional[SyncTriggerRequest] = None):
    """Trigger manual sync. Body is optional; a body-less POST uses defaults
    (strategy=auto, team=False, dry_run=False)."""
    request = body or SyncTriggerRequest()
    try:
        # Blocking throughout (subprocess + sqlite), so it goes to a worker
        # thread -- GET /status must stay answerable while a slow sync runs.
        outcome = await run_in_threadpool(
            sync_runner.run_sync_and_record,
            sync_mgr, history_mgr, conflict_mgr, quarantine_mgr,
            strategy=request.strategy,
            team=request.team,
            dry_run=request.dry_run,
        )

        if outcome.already_running:
            return SyncTriggerResponse(
                started=False,
                success=False,
                message="Sync already in progress"
            )

        if outcome.timed_out:
            # The run was still recorded (with whatever partial output the
            # killed subprocess produced) before this 504 -- a timed-out sync
            # must not vanish without a trace.
            raise HTTPException(
                status_code=504,
                detail=f"Sync timed out (recorded as run {outcome.run_id})",
            )

        await sync_runner.send_sync_notifications(outcome)

        result = outcome.result
        return SyncTriggerResponse(
            started=True,
            success=result.exit_code in (0, 3),
            exit_code=result.exit_code,
            run_id=outcome.run_id,
            message=f"Sync completed with exit code {result.exit_code}"
        )

    except HTTPException:
        raise
    except SyncEngineUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise _internal_error("POST /sync", e)


# ---------------------------------------------------------------------------
# /history
# ---------------------------------------------------------------------------

def _get_history(limit: int, offset: int, scope: Optional[str]) -> HistoryResponse:
    runs = history_mgr.get_recent(limit=limit, offset=offset, scope=scope)
    total = history_mgr.get_total_count(scope=scope)
    return HistoryResponse(runs=runs, total=total, limit=limit, offset=offset)


@router.get("/history", response_model=HistoryResponse)
async def get_history(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    scope: Optional[str] = None
):
    """Get sync history."""
    try:
        return await run_in_threadpool(_get_history, limit, offset, scope)
    except Exception as e:
        raise _internal_error("GET /history", e)


@router.get("/history/{run_id}")
async def get_run_details(run_id: int):
    """Get detailed sync run information."""
    try:
        details = await run_in_threadpool(history_mgr.get_run_details, run_id)
        if not details:
            raise HTTPException(status_code=404, detail="Run not found")
        return details

    except HTTPException:
        raise
    except Exception as e:
        raise _internal_error(f"GET /history/{run_id}", e)


# ---------------------------------------------------------------------------
# /conflicts
# ---------------------------------------------------------------------------

@router.get("/conflicts", response_model=ConflictsResponse)
async def get_conflicts():
    """Get unresolved conflicts."""
    try:
        conflicts = await run_in_threadpool(conflict_mgr.get_unresolved)
        return ConflictsResponse(
            conflicts=conflicts,
            total=len(conflicts)
        )

    except Exception as e:
        raise _internal_error("GET /conflicts", e)


@router.post("/conflicts/{conflict_id}/resolve")
async def resolve_conflict(conflict_id: int, resolution: ConflictResolution):
    """Resolve a conflict."""
    try:
        success = await run_in_threadpool(
            conflict_mgr.resolve_conflict, conflict_id, resolution.resolution
        )
        if not success:
            raise HTTPException(status_code=404, detail="Conflict not found")

        return {"success": True, "conflict_id": conflict_id, "resolution": resolution.resolution}

    except HTTPException:
        raise
    except Exception as e:
        raise _internal_error(f"POST /conflicts/{conflict_id}/resolve", e)


# ---------------------------------------------------------------------------
# /quarantine
# ---------------------------------------------------------------------------

@router.get("/quarantine", response_model=QuarantineResponse)
async def get_quarantine():
    """Get quarantined machines."""
    try:
        machines = await run_in_threadpool(quarantine_mgr.get_quarantined)
        return QuarantineResponse(
            machines=machines,
            total=len(machines)
        )

    except Exception as e:
        raise _internal_error("GET /quarantine", e)


@router.post("/quarantine/{machine}/clear")
async def clear_quarantine(machine: str):
    """Clear machine from quarantine."""
    try:
        success = await run_in_threadpool(quarantine_mgr.clear_quarantine, machine)
        if not success:
            raise HTTPException(status_code=404, detail="Machine not in quarantine")

        return {"success": True, "machine": machine}

    except HTTPException:
        raise
    except Exception as e:
        raise _internal_error(f"POST /quarantine/{machine}/clear", e)


# ---------------------------------------------------------------------------
# /backends
# ---------------------------------------------------------------------------

def _list_backends() -> BackendsResponse:
    current = backend_mgr.get_current_backend()
    available = backend_mgr.list_backends()
    return BackendsResponse(current=current, available=available)


@router.get("/backends", response_model=BackendsResponse)
async def list_backends():
    """List available backends."""
    try:
        return await run_in_threadpool(_list_backends)
    except Exception as e:
        raise _internal_error("GET /backends", e)


@router.post("/backends/test", response_model=BackendTestResult)
async def test_backend(body: BackendTestRequest):
    """Test backend connection."""
    try:
        return await run_in_threadpool(backend_mgr.test_connection, body.backend)
    except Exception as e:
        raise _internal_error("POST /backends/test", e)


@router.post("/backends/switch")
async def switch_backend(config: BackendConfig):
    """Switch to a different backend, applying any `config` values passed
    alongside it (validated against that backend's known settings) as part
    of the same write -- see BackendManager.switch_backend."""
    try:
        success = await run_in_threadpool(
            backend_mgr.switch_backend, config.backend, config.config
        )
        if not success:
            raise HTTPException(status_code=500, detail="Failed to switch backend")

        return {"success": True, "backend": config.backend}

    except HTTPException:
        raise
    except SyncEngineUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise _internal_error("POST /backends/switch", e)


@router.get("/backends/{name}/config", response_model=BackendConfigStatus)
async def get_backend_config(name: BackendName):
    """Effective settings for one backend -- which are explicitly set (in
    config.sh or the environment) vs. defaulted, with anything
    credential-shaped redacted. See BackendManager.get_backend_settings."""
    try:
        fields = await run_in_threadpool(backend_mgr.get_backend_settings, name)
        return BackendConfigStatus(backend=name, fields=fields)

    except Exception as e:
        raise _internal_error(f"GET /backends/{name}/config", e)


@router.put("/backends/{name}/config", response_model=BackendConfigStatus)
async def update_backend_config(name: BackendName, body: BackendConfigUpdate):
    """Persist settings for one backend into config.sh. Keys not known to
    belong to this backend are rejected (400) rather than written; values
    are safely quoted and validated before ever touching config.sh -- see
    BackendManager._write_config_values."""
    try:
        fields = await run_in_threadpool(
            backend_mgr.set_backend_config, name, body.config
        )
        return BackendConfigStatus(backend=name, fields=fields)

    except SyncEngineUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise _internal_error(f"PUT /backends/{name}/config", e)


# ---------------------------------------------------------------------------
# /daemon
# ---------------------------------------------------------------------------

@router.get("/daemon/config", response_model=DaemonConfig)
async def get_daemon_config():
    """Get daemon configuration."""
    try:
        config = await run_in_threadpool(sync_mgr.get_daemon_config)
        return DaemonConfig(**config)

    except Exception as e:
        raise _internal_error("GET /daemon/config", e)


@router.put("/daemon/config", response_model=DaemonConfig)
async def update_daemon_config(config: DaemonConfig):
    """Update daemon configuration."""
    try:
        updated = await run_in_threadpool(
            sync_mgr.update_daemon_config,
            enabled=config.enabled,
            scope=config.scope,
            interval=config.interval,
        )
        return DaemonConfig(**updated)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise _internal_error("PUT /daemon/config", e)


def _control_daemon(action: str) -> Tuple[bool, str]:
    config = sync_mgr.get_daemon_config()
    team = config["scope"] == "team"

    if action == "start":
        success, message = sync_mgr.start_daemon(team=team)
    elif action == "stop":
        success, message = sync_mgr.stop_daemon()
    else:
        success, message = sync_mgr.restart_daemon(team=team)

    if success:
        sync_mgr.update_daemon_config(enabled=action != "stop")

    return success, message


@router.post("/daemon/control")
async def control_daemon(control: DaemonControl):
    """
    Control the daemon (start/stop/restart) by driving the real lock/PID
    mechanism in lib/daemon.sh ($SYNC_ROOT/daemon.lock), and keep the
    daemon_state 'enabled' flag in sync with the outcome.
    """
    try:
        success, message = await run_in_threadpool(_control_daemon, control.action)
        return {"success": success, "action": control.action, "message": message}

    except SyncEngineUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise _internal_error("POST /daemon/control", e)


# Mount every `/api/...` route defined above onto the app. Must come after
# all `@router...` decorators run (i.e. at module scope, after the route
# definitions), and is deliberately separate from `/health`, which is
# registered directly on `app` above -- see the comment there.
app.include_router(router)


# ---------------------------------------------------------------------------
# Startup log block
# ---------------------------------------------------------------------------
#
# One INFO block, logged once at import time (which for a uvicorn-served app
# is effectively process startup), that alone should answer "is this the
# build I think it is, and can it see the sync engine" without any further
# digging: the resolved data dir (where backend.log and history.db live --
# confirms which KIROCREW_DIR/KIROCREW_SYNC_DB this process actually
# resolved, which has been a real source of confusion when the gateway's
# environment differs from a manually-started shell's), the resolved
# sync-engine directory and whether kirocrew-sync.sh was actually found
# there, and the full list of routes this process registered (confirms
# "byte-identical build" suspicions one way or the other -- a stale process
# serving an older server.py would be missing routes a newer repo checkout
# has, or vice versa).
def _log_startup_info() -> None:
    from fastapi.routing import APIRoute

    data_dir = sync_mgr.db.db_path.parent
    # FastAPI's app.include_router(router) does NOT flatten the included
    # router's individual APIRoute objects into app.routes -- it wraps them
    # in a single opaque `_IncludedRouter` entry instead (verified against
    # the installed fastapi version; app.routes directly exposes only
    # /health, /docs, /openapi.json, etc.). The `/api/*` routes are still
    # exactly `router.routes` though (already carrying the "/api" prefix,
    # since `router` was constructed with `prefix="/api"`), so read the
    # actual per-route list from there instead of trying to walk the
    # opaque wrapper.
    all_routes = [r for r in app.routes if isinstance(r, APIRoute)] + [
        r for r in router.routes if isinstance(r, APIRoute)
    ]
    api_routes = sorted(
        (r.path, ",".join(sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS"))))
        for r in all_routes
    )

    logger.info("=" * 70)
    logger.info("KiroCrew Sync backend starting up")
    logger.info("  data dir:          %s", data_dir)
    logger.info("  sync-engine dir:   %s", sync_mgr.sync_dir)
    logger.info(
        "  sync engine found: %s (script: %s)", sync_mgr.available, sync_mgr.script
    )
    logger.info("  log level:         %s", logging.getLevelName(logger.getEffectiveLevel()))
    logger.info("  registered routes (%d):", len(api_routes))
    for path, methods in api_routes:
        logger.info("    %-7s %s", methods, path)
    logger.info("=" * 70)


_log_startup_info()
