"""
Logging setup for the KiroCrew Sync app backend.

Configures the *root* Python logger -- not a dedicated named logger -- so
every module in this app that does the ordinary `logging.getLogger(__name__)`
thing (server.py, database.py, sync_manager.py, history.py, conflicts.py,
quarantine.py, backends.py, notifications.py, ...) ends up in one place via
normal propagation, with no per-module wiring required and no need to touch
those other modules.

Output goes to two places at once:
  - stderr, which the KiroCrew gateway captures from the backend process it
    spawns (this is what shows up when the gateway itself is inspected).
  - a rotating file at <data-dir>/backend.log, so a request that fails
    silently from the browser's point of view still leaves a paper trail on
    disk even if nobody happened to be watching stderr at the time.

Debugging a live install should not require redeploying with more print()
calls -- it should just require looking at this file.
"""

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Optional

# Same env var convention server.py's _resolve_db_path() uses for the app's
# sqlite database. Deliberately re-implemented here rather than imported
# from server.py: this module must be configurable standalone, before (and
# independently of) the rest of the app importing anything, and must not
# create an import cycle with server.py (which imports *this* module).
_KIROCREW_SYNC_DB_ENV = "KIROCREW_SYNC_DB"
_KIROCREW_DIR_ENV = "KIROCREW_DIR"

# Configurable per TASK A.4 -- documented in app/README.md.
LOG_LEVEL_ENV = "KIROCREW_SYNC_LOG_LEVEL"

LOG_FILE_NAME = "backend.log"
LOG_FILE_MAX_BYTES = 2 * 1024 * 1024  # 2MB
LOG_FILE_BACKUP_COUNT = 3

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# Marker attribute set on every handler this module adds, so configure_logging()
# can find (and replace) only its own handlers on a second call -- e.g. when
# tests reload backend.server -- without disturbing handlers other code
# (pytest's caplog, uvicorn, etc.) may have attached to the root logger.
_HANDLER_MARKER = "_kirocrew_sync_logging"


def resolve_data_dir() -> Path:
    """
    Resolve the app's data directory.

    Mirrors _resolve_db_path() in server.py (the app's own database path
    resolution), which itself honors the same KIROCREW_DIR override
    SyncManager uses, plus the KIROCREW_SYNC_DB escape hatch:

      1. KIROCREW_SYNC_DB set -> its parent directory (an explicit db file
         location implies its directory is the data dir).
      2. KIROCREW_DIR set -> <KIROCREW_DIR>/apps/kirocrew-sync/data.
      3. Neither set -> ~/.kiro/crew/apps/kirocrew-sync/data, the same
         default database.py's Database() falls back to.
    """
    db_override = os.environ.get(_KIROCREW_SYNC_DB_ENV)
    if db_override:
        return Path(db_override).parent

    kirocrew_dir = os.environ.get(_KIROCREW_DIR_ENV)
    if kirocrew_dir:
        return Path(kirocrew_dir) / "apps" / "kirocrew-sync" / "data"

    return Path.home() / ".kiro" / "crew" / "apps" / "kirocrew-sync" / "data"


def configure_logging(level: Optional[str] = None) -> logging.Logger:
    """
    Configure the root logger with a stderr handler (always) plus a
    best-effort rotating file handler at <data-dir>/backend.log.

    `level` overrides KIROCREW_SYNC_LOG_LEVEL, which overrides the default
    of INFO. An unrecognized level name falls back to INFO rather than
    raising, since a typo'd env var must not prevent the app from starting.

    Idempotent: safe to call more than once (e.g. once per test via
    importlib.reload(server_module)) without stacking duplicate handlers --
    a second call replaces the handlers *this* function previously added
    and leaves any others (pytest's caplog handler, etc.) alone.

    Never raises. If the file handler can't be created -- read-only data
    dir, an uncreatable parent, disk full -- this logs a warning to stderr
    and continues with stderr-only logging rather than taking the whole app
    down over a logging problem.
    """
    level_name = (level or os.environ.get(LOG_LEVEL_ENV) or "INFO").upper()
    resolved_level = getattr(logging, level_name, None)
    if not isinstance(resolved_level, int):
        resolved_level = logging.INFO

    root = logging.getLogger()
    root.setLevel(resolved_level)

    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)

    formatter = logging.Formatter(_LOG_FORMAT)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    setattr(stream_handler, _HANDLER_MARKER, True)
    root.addHandler(stream_handler)

    log_path = None
    try:
        data_dir = resolve_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        log_path = data_dir / LOG_FILE_NAME
        file_handler = logging.handlers.RotatingFileHandler(
            str(log_path),
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        setattr(file_handler, _HANDLER_MARKER, True)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning(
            "Could not open backend log file at %s (%s); logging to stderr only",
            log_path, exc,
        )

    return root
