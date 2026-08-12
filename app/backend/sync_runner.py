"""
Shared "run a sync and record everything" path.

This module exists so that two very different entry points -- the HTTP
`POST /api/sync` route in server.py (a manual "Sync Now" click) and the
standalone cron CLI in cli.py (a background tick) -- produce byte-identical
database state for the same engine result: same `sync_runs` row, same
conflict/quarantine ingestion, same `daemon_state` bookkeeping, same
notifications. Before cli.py existed, only the HTTP route did any of this,
which is exactly why cron-triggered syncs used to vanish from the app
entirely (see cli.py's module docstring for the full story).

`run_sync_and_record` is deliberately synchronous and side-effect-only: it
does the real subprocess call and sqlite writes, and returns a `SyncOutcome`
describing what happened. It does NOT send notifications itself, because
notifications are async (`NotificationService.notify_*` are coroutines) and
this function is not -- see `send_sync_notifications` below, an async
coroutine callers await separately after inspecting the outcome. Splitting
it this way also keeps this function trivially callable from a context with
no event loop at all (the CLI's `main()`), and from one that already has one
(server.py, which must run this off the loop via `run_in_threadpool` to stay
responsive -- see test_status_stays_responsive_during_a_slow_sync in
test_server.py).
"""

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import artifacts
from .conflicts import ConflictManager
from .history import HistoryManager
from .models import SyncChange, SyncResult
from .notifications import get_notification_service
from .quarantine import QuarantineManager
from .sync_manager import SyncManager

logger = logging.getLogger(__name__)


def resolve_db_path() -> Optional[Path]:
    """
    Resolve where the app's own SQLite database lives.

    Lives here, in the module both entry points already import, so the HTTP
    route and the cron CLI cannot drift onto different databases -- which
    would be a silent and deeply confusing failure: the cron would record
    runs the dashboard never shows. Honors the same KIROCREW_DIR override
    SyncManager uses for the engine's data, plus a dedicated
    KIROCREW_SYNC_DB escape hatch. None keeps each manager's own default
    (~/.kiro/crew/apps/kirocrew-sync/data/history.db).
    """
    override = os.environ.get("KIROCREW_SYNC_DB")
    if override:
        return Path(override)
    kirocrew_dir = os.environ.get("KIROCREW_DIR")
    if kirocrew_dir:
        return Path(kirocrew_dir) / "apps" / "kirocrew-sync" / "data" / "history.db"
    return None

# Shape returned by SyncManager.ingest_artifacts, and what a caller gets back
# when ingestion was skipped or never reached (already-running / timeout /
# pre-truncation exit). Kept as a module-level constant so every "nothing was
# ingested" path returns an identically-shaped dict rather than three
# hand-rolled copies.
_EMPTY_INGEST: Dict[str, list] = {
    "conflicts": [],
    "quarantine": [],
    "new_unresolved_conflicts": [],
    "new_quarantine": [],
}


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
_SYNC_TRUNCATION_MARKER = "Unpacking local state"


def _sync_reached_truncation(result: SyncResult) -> bool:
    """Whether conflicts.jsonl/quarantine.txt were (re)truncated by this
    run, i.e. whether it is safe to ingest them as describing this run."""
    if result.exit_code in (0, 3):
        return True
    return _SYNC_TRUNCATION_MARKER in (result.output or "")


def _derive_changes(sync_mgr: SyncManager, result: SyncResult, team: bool) -> List[SyncChange]:
    """
    The sync_changes rows this run's output honestly supports (see
    artifacts.derive_sync_changes for exactly what is and is not derivable).

    Reads conflicts.jsonl directly rather than waiting for
    ingest_artifacts() to hand back its parsed records, because record_sync()
    -- which needs this list -- has to run before ingest_artifacts() can (it
    produces the run_id ingest_artifacts() writes conflicts against). This
    mirrors SyncManager._artifact_counts(), which already re-reads the same
    file independently, for the same reason: cmd_sync() truncates
    conflicts.jsonl once at the very start of the run and nothing rewrites it
    before this function or ingest_artifacts() gets to read it, so reading it
    twice yields identical records, not stale or duplicated ones.
    """
    paths = artifacts.scope_paths(sync_mgr.sync_root, team)
    conflict_records = artifacts.read_conflicts(paths["conflicts"])
    return artifacts.derive_sync_changes(result.output, conflict_records)


def _cleanup_history_safe(history_mgr: HistoryManager) -> None:
    """Best-effort trim of old sync_runs rows. Never lets a cleanup failure
    fail the run that triggered it -- see cleanup_old_runs() in history.py."""
    try:
        history_mgr.cleanup_old_runs()
    except Exception:
        logger.exception("history cleanup_old_runs failed")


@dataclass
class SyncOutcome:
    """Everything that happened for one sync attempt, in a form both the
    HTTP route and the CLI can turn into their own response/exit-code
    contract without re-deriving it.

    started:
        False only for the already-running short-circuit -- no engine
        process ran at all, nothing was recorded.
    already_running:
        A sync was already in progress; this attempt was skipped entirely.
    timed_out:
        The engine process was killed after exceeding `timeout`. `result`
        is still populated (a synthetic exit_code=-1 SyncResult) and the
        run WAS recorded, mirroring the HTTP route's existing behavior of
        never letting a timeout vanish without a trace.
    result / run_id / ingest:
        The recorded SyncResult, its `sync_runs` row id, and whatever
        ingest_artifacts() returned (or `_EMPTY_INGEST` if ingestion was
        skipped or not reached).
    """
    started: bool
    already_running: bool = False
    timed_out: bool = False
    result: Optional[SyncResult] = None
    run_id: Optional[int] = None
    ingest: Dict[str, list] = field(default_factory=lambda: dict(_EMPTY_INGEST))
    timeout_seconds: Optional[float] = None


def run_sync_and_record(
    sync_mgr: SyncManager,
    history_mgr: HistoryManager,
    conflict_mgr: ConflictManager,
    quarantine_mgr: QuarantineManager,
    *,
    strategy: str = "auto",
    team: bool = False,
    dry_run: bool = False,
    timeout: int = 300,
) -> SyncOutcome:
    """
    Run one sync via the bash engine and record everything the app needs to
    show it happened: a `sync_runs` row, conflict/quarantine ingestion, the
    `daemon_state` last_run/next_run bookkeeping, and history pruning.

    Blocking throughout (subprocess.run, sqlite) -- entirely synchronous on
    purpose, so it works the same whether or not the caller has an event
    loop. Raises `SyncEngineUnavailable` (from sync_mgr.run_sync) exactly
    like the underlying call does; callers are expected to catch that
    themselves (server.py maps it to a 503, cli.py to a non-zero exit).
    """
    if sync_mgr.is_running():
        return SyncOutcome(started=False, already_running=True)

    start = time.time()
    try:
        result = sync_mgr.run_sync(
            strategy=strategy, team=team, dry_run=dry_run, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        # A timed-out sync previously vanished entirely: the caller's own
        # timeout handling ran before record_sync(), so there was no
        # history row, no ingestion, and the (killed) engine left no trace
        # at all. Record it as a failed run -- with whatever partial output
        # the subprocess had produced before being killed, if any.
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
            scope="team" if team else "personal",
            strategy=strategy,
            dry_run=dry_run,
            changes_detected=False,
            rows_merged=0,
            conflicts_count=0,
            quarantine_count=0,
            output=(partial_stdout or "") + (partial_stderr or ""),
            error=f"Sync timed out after {exc.timeout}s and was killed",
        )
        run_id = history_mgr.record_sync(timeout_result)
        _cleanup_history_safe(history_mgr)
        return SyncOutcome(
            started=True,
            timed_out=True,
            result=timeout_result,
            run_id=run_id,
            timeout_seconds=exc.timeout,
        )

    # Change derivation reads conflicts.jsonl (see _derive_changes' docstring
    # for why it cannot simply wait for ingest_artifacts()'s copy), gated by
    # the same truncation check ingest_artifacts() uses below -- otherwise a
    # run that exited before cmd_sync() ever truncated the log would read
    # (and record as its own) a previous run's leftover conflicts.
    changes: List[SyncChange] = []
    if _sync_reached_truncation(result):
        try:
            changes = _derive_changes(sync_mgr, result, team)
        except Exception:
            logger.exception(
                "Change derivation failed for this run; sync result is "
                "still recorded"
            )

    run_id = history_mgr.record_sync(result, changes=changes)

    # Ingestion is a separate, non-atomic step from recording the run: if it
    # raises, the sync itself already succeeded (or failed) and was already
    # recorded -- that result must still be reported honestly rather than
    # this function raising for a sync that actually completed.
    ingest = dict(_EMPTY_INGEST)
    if _sync_reached_truncation(result):
        try:
            ingest = sync_mgr.ingest_artifacts(
                run_id=run_id,
                team=team,
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

    sync_mgr.record_daemon_run()
    _cleanup_history_safe(history_mgr)

    return SyncOutcome(started=True, result=result, run_id=run_id, ingest=ingest)


async def _notify(coro_factory) -> None:
    """Run a single notification call, never letting it break the caller."""
    try:
        svc = get_notification_service()
        await coro_factory(svc)
    except Exception:
        # Notifications are best-effort; a broken notification channel must
        # never fail a sync that otherwise succeeded (or was already
        # recorded as failed).
        pass


async def send_sync_notifications(outcome: SyncOutcome) -> None:
    """Fire the same notifications a completed sync always fires: failure,
    quarantine, conflict, and the "completed with issues" summary.

    A no-op for `already_running` and `timed_out` outcomes, matching the
    HTTP route's pre-existing behavior (neither case ever notified before
    this function existed -- the timeout path returned straight to a 504,
    and the already-running path returned straight to the caller). Preserved
    here rather than "fixed", since changing that is out of scope for this
    change and would make the CLI's behavior diverge from the route's.
    """
    if not outcome.started or outcome.timed_out or outcome.result is None:
        return

    result = outcome.result
    run_id = outcome.run_id
    ingest = outcome.ingest

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
