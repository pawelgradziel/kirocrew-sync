"""
FastAPI server for KiroCrew Sync app.
"""

import logging
import os
import subprocess
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
    BackendName, BackendConfigStatus, BackendConfigUpdate, SyncResult
)
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

# Notifications are owned by a different work package and may not exist yet
# (or may fail to import while it's being written). Importing defensively
# means this module -- and the whole app -- stays importable either way, and
# notification calls become no-ops until that module lands.
try:
    from .notifications import get_notification_service
except ImportError:
    get_notification_service = None


def _resolve_db_path() -> Optional[Path]:
    """
    Resolve where the app's own SQLite database lives, honoring the same
    KIROCREW_DIR override SyncManager uses for the sync engine's data, plus
    a dedicated KIROCREW_SYNC_DB escape hatch. None keeps each manager's own
    hardcoded default (~/.kiro/crew/apps/kirocrew-sync/data/history.db).
    """
    override = os.environ.get("KIROCREW_SYNC_DB")
    if override:
        return Path(override)
    kirocrew_dir = os.environ.get("KIROCREW_DIR")
    if kirocrew_dir:
        return Path(kirocrew_dir) / "apps" / "kirocrew-sync" / "data" / "history.db"
    return None


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
# Notifications
# ---------------------------------------------------------------------------

async def _notify(coro_factory):
    """Run a single notification call, never letting it break the caller."""
    if get_notification_service is None:
        return
    try:
        svc = get_notification_service()
        await coro_factory(svc)
    except Exception:
        # Notifications are best-effort; a broken notification channel must
        # never fail a sync request that otherwise succeeded.
        pass


async def _send_sync_notifications(result, run_id: int, ingest: dict) -> None:
    if result.exit_code not in (0, 3):
        await _notify(lambda svc: svc.notify_failure(
            result.error or f"Sync failed with exit code {result.exit_code}",
            run_id=run_id,
        ))

    for machine in ingest.get("new_quarantine", []):
        await _notify(lambda svc, m=machine: svc.notify_quarantine(
            m, "Quarantined by the sync engine (version, embedding, or scope mismatch)"
        ))

    for conflict in ingest.get("new_unresolved_conflicts", []):
        await _notify(lambda svc, c=conflict: svc.notify_conflict(c))

    await _notify(lambda svc: svc.notify_sync_completed(
        run_id=run_id,
        rows_merged=result.rows_merged,
        conflicts=len(ingest.get("conflicts", [])),
        quarantine=len(ingest.get("quarantine", [])),
    ))


# ---------------------------------------------------------------------------
# Sync-artifact ingestion safety
# ---------------------------------------------------------------------------

# cmd_sync() in kirocrew-sync.sh truncates conflicts.jsonl/quarantine.txt as
# literally its first action (kirocrew-sync.sh:412-413), then immediately
# logs "Unpacking local state..." (kirocrew-sync.sh:422) before doing
# anything else. So exit codes 0 and 3 always come from a run that reached
# (and got well past) that truncation. Exit code 1, though, can come from
# *before* cmd_sync ever ran at all -- an invalid --strategy/--scope value,
# or missing python3/git (require_python/require_git) -- in which case
# conflicts.jsonl/quarantine.txt are untouched leftovers from whatever run
# last completed, and ingesting them would re-record that old run's
# conflicts/quarantine as if they were newly discovered, without bound.
#
# The strategy case is now closed off at the API boundary (SyncTriggerRequest
# .strategy is a Literal, so a bad value never reaches run_sync at all -- see
# models.py). The remaining pre-truncation exit-1 paths are environment
# failures (python3/git missing or broken), not reachable through this API's
# own parameters. SyncManager/artifacts.py are owned by a different work
# package and are intentionally not edited here (see the review notes this
# fixes), so this marker check is the best signal available from the
# captured subprocess output alone, as defense in depth for that residual
# case. A more robust fix belongs in SyncManager/kirocrew-sync.sh itself:
# have cmd_sync emit an explicit machine-readable marker (or SyncManager
# track whether the subprocess got past argument parsing), rather than
# server.py pattern-matching a log line.
_SYNC_TRUNCATION_MARKER = "Unpacking local state"


def _sync_reached_truncation(result: SyncResult) -> bool:
    """Whether conflicts.jsonl/quarantine.txt were (re)truncated by this
    run, i.e. whether it is safe to ingest them as describing this run."""
    if result.exit_code in (0, 3):
        return True
    return _SYNC_TRUNCATION_MARKER in (result.output or "")


def _cleanup_history_safe() -> None:
    """Best-effort trim of old sync_runs rows. Never lets a cleanup failure
    fail the request that triggered it -- see cleanup_old_runs() in
    history.py, which this app previously never called at all, so the
    output/error text of every run accumulated in the DB unbounded."""
    try:
        history_mgr.cleanup_old_runs()
    except Exception:
        logger.exception("history cleanup_old_runs failed")


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
        # mirrors _parse_output()/_send_sync_notifications() in
        # sync_manager.py/server.py, which already treat (0, 3) as success.
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
        already_running = await run_in_threadpool(sync_mgr.is_running)
        if already_running:
            return SyncTriggerResponse(
                started=False,
                success=False,
                message="Sync already in progress"
            )

        start = time.time()
        try:
            result = await run_in_threadpool(
                sync_mgr.run_sync,
                strategy=request.strategy,
                team=request.team,
                dry_run=request.dry_run,
            )
        except subprocess.TimeoutExpired as exc:
            # A timed-out sync previously vanished entirely: the 504 was
            # raised before record_sync(), so there was no history row, no
            # ingestion, and the (killed) engine left no trace at all. Record
            # it as a failed run -- with whatever partial output the
            # subprocess had produced before being killed, if any -- before
            # still reporting the timeout to the caller.
            duration_ms = int((time.time() - start) * 1000)
            partial_stdout = exc.stdout if isinstance(exc.stdout, str) else (
                exc.stdout.decode("utf-8", "replace") if exc.stdout else ""
            )
            partial_stderr = exc.stderr if isinstance(exc.stderr, str) else (
                exc.stderr.decode("utf-8", "replace") if exc.stderr else ""
            )
            timeout_result = SyncResult(
                exit_code=-1,
                duration_ms=duration_ms,
                scope="team" if request.team else "personal",
                strategy=request.strategy,
                dry_run=request.dry_run,
                changes_detected=False,
                rows_merged=0,
                conflicts_count=0,
                quarantine_count=0,
                output=(partial_stdout or "") + (partial_stderr or ""),
                error=f"Sync timed out after {exc.timeout}s and was killed",
            )
            run_id = await run_in_threadpool(history_mgr.record_sync, timeout_result)
            await run_in_threadpool(_cleanup_history_safe)
            raise HTTPException(
                status_code=504,
                detail=f"Sync timed out (recorded as run {run_id})",
            )

        run_id = await run_in_threadpool(history_mgr.record_sync, result)

        # Ingestion is a separate, non-atomic step from recording the run:
        # if it raises, the sync itself already succeeded (or failed) and
        # was already recorded -- that result must still be reported
        # honestly rather than turning into a 500 for a sync that actually
        # completed. Log it and move on with an empty ingest.
        ingest = {
            "conflicts": [], "quarantine": [],
            "new_unresolved_conflicts": [], "new_quarantine": [],
        }
        if _sync_reached_truncation(result):
            try:
                ingest = await run_in_threadpool(
                    sync_mgr.ingest_artifacts,
                    run_id=run_id,
                    team=request.team,
                    conflict_mgr=conflict_mgr,
                    quarantine_mgr=quarantine_mgr,
                )
            except Exception:
                logger.exception(
                    "Artifact ingestion failed for run %s; sync result is "
                    "still reported to the caller", run_id,
                )
        else:
            logger.warning(
                "Run %s exited %s before cmd_sync reached truncation; "
                "skipping artifact ingestion to avoid re-recording a "
                "previous run's leftover conflicts.jsonl/quarantine.txt",
                run_id, result.exit_code,
            )

        await run_in_threadpool(sync_mgr.record_daemon_run)
        await run_in_threadpool(_cleanup_history_safe)

        await _send_sync_notifications(result, run_id, ingest)

        return SyncTriggerResponse(
            started=True,
            success=result.exit_code in (0, 3),
            exit_code=result.exit_code,
            run_id=run_id,
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
