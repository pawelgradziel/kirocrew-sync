#!/usr/bin/env python3
"""
Standalone sync entry point, for the app's background cron.

WHY THIS EXISTS
---------------
The cron declared in app.json originally carried a ``message``, which makes
KiroCrew dispatch it as an *agent prompt*: every tick spun up an ACP session
and asked a language model to go and run the sync. That burned tokens, failed
outright whenever the model was unavailable, and re-asked for tool approvals
on every tick (each run being a fresh session, approving could never stick).

Switching the cron to a ``command`` fixes all of that -- but a command cron
cannot call this app's own ``POST /api/sync`` route: it cannot mint a gateway
token (that needs ``$(...)``, which KiroCrew's ``_vet_shell_command`` bans
precisely so a cron cannot read credential files) and it cannot forge the
proxy's per-app HMAC, whose secret is only injected into the backend
process's own environment. So the cron would have had to shell straight into
``kirocrew-sync.sh``, which syncs correctly but records *nothing* in the app:
no ``sync_runs`` row, no conflict/quarantine ingestion, no notifications.

This module is the way out: it skips HTTP entirely and calls the very same
``sync_runner.run_sync_and_record`` the HTTP route calls, so a cron tick and
a "Sync Now" click leave identical state behind.

RUNTIME CONSTRAINTS (both real, both load-bearing)
--------------------------------------------------
1. A cron ``command`` inherits the *gateway's* working directory, not this
   app's, so this file cannot rely on cwd and cannot use relative imports --
   it is executed as a plain script (``python3 .../backend/cli.py``), not as
   ``python -m backend.cli``, so ``from .sync_runner import ...`` would fail
   with "attempted relative import with no known parent package". Hence the
   sys.path bootstrap below and absolute ``backend.*`` imports.
2. The environment is cleaned (KiroCrew strips some variables before running
   a cron command), so nothing here may depend on inherited secrets.

EXIT CODES
----------
0 on success, non-zero on failure, because a non-zero exit is what makes
KiroCrew record a failed cron run (and auto-pause the job after five
consecutive failures). Note that engine exit code **3** is a SUCCESS here:
per kirocrew-sync.sh's contract it means "sync completed, but one or more
machines stayed quarantined". Treating it as failure would auto-pause the
cron after five ticks that all worked exactly as designed.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# --- sys.path bootstrap -----------------------------------------------------
# Must happen before any `backend.*` import. `__file__` is
# <app dir>/backend/cli.py, so parents[1] is <app dir> -- the directory that
# has to be importable for `backend` to resolve as a package. Resolve first so
# a symlinked install still points at the real tree.
_APP_DIR = Path(__file__).resolve().parents[1]
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from backend import sync_runner  # noqa: E402  (import must follow the bootstrap)
from backend.conflicts import ConflictManager  # noqa: E402
from backend.history import HistoryManager  # noqa: E402
from backend.logging_setup import CRON_LOG_FILE_NAME, configure_logging  # noqa: E402
from backend.quarantine import QuarantineManager  # noqa: E402
from backend.sync_manager import SyncEngineUnavailable, SyncManager  # noqa: E402

logger = logging.getLogger("backend.cli")

# Engine exit codes that mean "the sync did its job". 3 == completed with
# machine(s) still quarantined; see kirocrew-sync.sh's usage text.
_SUCCESS_EXIT_CODES = (0, 3)

# Deliberately below the 300s ceiling KiroCrew imposes on a command cron
# (`cmd_timeout = job.timeout or 300` in slack/gateway.py, handed to
# cron_script.run_command_sandboxed, which kills the whole process group when
# it elapses -- and the app manifest sets no per-job timeout, so 300 it is).
# At 300 the two deadlines race and the gateway's kill wins: the engine dies
# with nothing written, which is precisely the invisibility this module exists
# to fix. Timing out first instead means run_sync_and_record still gets to
# record the failed run, and the margin covers that write.
DEFAULT_TIMEOUT_SECONDS = 240


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kirocrew-sync-app",
        description="Run one sync and record it, exactly as POST /api/sync does.",
    )
    parser.add_argument(
        "--strategy",
        default="auto",
        choices=["auto", "local-wins", "remote-wins", "manual"],
        help="conflict resolution strategy (default: auto)",
    )
    parser.add_argument(
        "--team", action="store_true", help="sync the team scope instead of personal"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="run the engine without writing changes"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"engine timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    # cron.log, not the server's backend.log: the backend process is long-lived
    # and this one is spawned fresh every tick, so the two genuinely overlap --
    # and two processes sharing one RotatingFileHandler lose log segments, since
    # rotation renames the file out from under whichever process did not trigger
    # it. See logging_setup.CRON_LOG_FILE_NAME.
    configure_logging(file_name=CRON_LOG_FILE_NAME)

    db_path = sync_runner.resolve_db_path()
    logger.info(
        "cron sync starting (strategy=%s team=%s dry_run=%s db=%s)",
        args.strategy, args.team, args.dry_run, db_path or "<default>",
    )

    try:
        outcome = sync_runner.run_sync_and_record(
            SyncManager(db_path=db_path),
            HistoryManager(db_path=db_path),
            ConflictManager(db_path=db_path),
            QuarantineManager(db_path=db_path),
            strategy=args.strategy,
            team=args.team,
            dry_run=args.dry_run,
            timeout=args.timeout,
        )
    except SyncEngineUnavailable as exc:
        # The bash engine is not installed where we expect it. A real failure
        # worth surfacing as a failed cron run, not something to swallow.
        logger.error("cron sync could not run: %s", exc)
        return 1
    except Exception:
        logger.exception("cron sync failed with an unexpected error")
        return 1

    if outcome.already_running:
        # Not a failure: a sync was in flight (a manual one, or a slow
        # previous tick). Exiting non-zero here would rack up "failures" for
        # the job and eventually auto-pause a perfectly healthy daemon.
        logger.info("cron sync skipped: a sync is already in progress")
        return 0

    if outcome.timed_out:
        logger.error(
            "cron sync timed out after %ss (recorded as run %s)",
            outcome.timeout_seconds, outcome.run_id,
        )
        return 1

    # Notifications are async and best-effort; run_sync_and_record deliberately
    # does not send them itself. Mirrors what the HTTP route does after it
    # returns, so a cron tick notifies exactly like a "Sync Now" click.
    try:
        asyncio.run(sync_runner.send_sync_notifications(outcome))
    except Exception:
        logger.exception("cron sync: notifications failed (sync itself was recorded)")

    result = outcome.result
    exit_code = result.exit_code if result else 1
    ok = exit_code in _SUCCESS_EXIT_CODES
    logger.info(
        "cron sync finished: run=%s engine_exit=%s rows_merged=%s conflicts=%s "
        "quarantined=%s -> %s",
        outcome.run_id, exit_code,
        getattr(result, "rows_merged", "?"),
        len(outcome.ingest.get("conflicts", [])),
        len(outcome.ingest.get("quarantine", [])),
        "ok" if ok else "FAILED",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
