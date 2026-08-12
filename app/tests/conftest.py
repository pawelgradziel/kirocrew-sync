"""
Shared pytest fixtures for the app backend test suite.

SAFETY: these tests must never touch the user's real KiroCrew data directory
(``~/.kiro/crew``). Two independent layers guarantee that:

1. We never import the ``backend`` package (``app/backend/__init__.py``) as a
   package. That ``__init__.py`` does ``from .server import app``, which
   transitively constructs ``SyncManager``/``HistoryManager`` with no
   explicit ``db_path`` -- and ``Database.__init__`` defaults to
   ``Path.home() / ".kiro/crew/apps/kirocrew-sync/data/history.db"``,
   creating that directory as a side effect of merely *importing* the
   package (verified by hand: `python -c "import backend"` from ``app/``
   writes to the real ``~/.kiro/crew/apps/kirocrew-sync/data/`` before any
   test even runs). We work around this by registering a lightweight stand-in
   module object as ``sys.modules["backend"]`` with the real backend
   directory on its ``__path__`` *before* anything imports a `backend.*`
   submodule. Python's import system sees the package already "loaded" and
   never executes the real ``__init__.py``; ordinary submodule imports
   (``backend.database``, ``backend.history``, ...) and the relative imports
   those modules use internally (``from .database import Database``) work
   exactly as normal, since they resolve through ``__path__``.

   This also happens to be *why* this suite never imports ``backend.server``
   or ``backend.backends`` at all: those are out of scope for this work
   package (owned by another concurrent change) and this trick would not
   protect against their own import-time side effects if something imported
   them anyway.

2. Belt and suspenders: an autouse fixture monkeypatches ``pathlib.Path.home``
   for the duration of every test to a per-test temporary directory. Every
   manager in this codebase that is handed no explicit path falls back to
   ``Path.home() / ".kiro/..."``, so even a future test that forgets to pass
   an explicit ``db_path``/``sync_dir``/``kirocrew_dir`` cannot resolve to the
   real home directory -- it would land in a throwaway tmp dir instead, not
   silently succeed against real user data.

Every test in this suite additionally passes explicit tmp_path-derived paths
to every constructor, so layer 2 is a safety net, not the primary mechanism.
"""

import sys
import types
from pathlib import Path

import pytest

_APP_DIR = Path(__file__).resolve().parents[1]  # .../app
_BACKEND_DIR = _APP_DIR / "backend"

if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

if "backend" not in sys.modules:
    _stub = types.ModuleType("backend")
    _stub.__path__ = [str(_BACKEND_DIR)]
    _stub.__package__ = "backend"
    sys.modules["backend"] = _stub


@pytest.fixture(autouse=True)
def _fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() to a throwaway directory for every test.

    Safety net only -- see module docstring. Individual tests still pass
    explicit paths to every Database/Manager constructor.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return fake_home


@pytest.fixture
def db_path(tmp_path) -> Path:
    """A fresh, never-yet-created sqlite path under the test's tmp_path."""
    return tmp_path / "data" / "history.db"
