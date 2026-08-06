"""Knowledge-path portability, applied at the row level.

`lib/portable_paths.py` owns the translation rules (see ADR 0001). This module
applies them where the sync boundary now lives: unpack encodes paths to their
portable form on the way out, pack decodes them back to this machine's absolute
paths on the way in.

Doing it per row rather than on a copy of the database has a second benefit —
the portable form is what the merge sees, so two machines that added the same
folder under different local paths resolve to one row instead of two competing
ones. That applies to `folder_file_state` too, whose primary key contains the
path: encoding happens before row identity is computed, so both machines agree
on which row is which.
"""

import os
import sys
from pathlib import Path

# portable_paths.py sits one level up, beside this package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import portable_paths
    AVAILABLE = True
except ImportError:                                   # pragma: no cover
    portable_paths = None
    AVAILABLE = False

# (database, table) -> the column carrying a filesystem path.
# Mirrors portable_paths.TARGETS.
PATH_COLUMNS = {
    ("knowledge", "sources"): "uri",
    ("knowledge", "folder_file_state"): "file_path",
    ("knowledge", "dismissed_auto_sources"): "uri",
}


def enabled():
    """Path translation is on unless explicitly disabled or unavailable."""
    return AVAILABLE and os.environ.get("SYNC_PORTABLE_PATHS", "1") != "0"


def load_mappings():
    if not AVAILABLE:
        return []
    try:
        return portable_paths.load_mappings(os.environ.get("KIROCREW_PATH_MAP"))
    except Exception:
        # A malformed map must not take the sync down; paths travel verbatim.
        return []


def column_for(db_name, table):
    return PATH_COLUMNS.get((db_name, table))


def encode(value, mappings, home=None):
    """Local absolute path -> portable form. Idempotent."""
    if not enabled() or not isinstance(value, str) or not value:
        return value
    return portable_paths.to_portable(
        value, mappings, home or os.path.expanduser("~"))


def decode(value, mappings, home=None):
    """Portable form -> this machine's absolute path. Returns (value, warning)."""
    if not enabled() or not isinstance(value, str) or not value:
        return value, None
    return portable_paths.to_local(
        value, mappings, home or os.path.expanduser("~"))
