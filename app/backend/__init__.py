"""
KiroCrew Sync app backend.

Deliberately empty of imports. This package used to do ``from .server import
app``, which meant importing *any* submodule -- ``backend.models``,
``backend.notifications`` -- transitively built the FastAPI app and every
manager, and those managers create their SQLite database on construction. The
practical effect was that merely importing this package wrote into the user's
real ``~/.kiro/crew/apps/kirocrew-sync/data/`` directory as an import side
effect.

Nothing needs the re-export: app.json declares ``backend.entryPoint`` as
``backend.server:app``, so the gateway imports ``backend.server`` by name.
Import the submodule you actually want.
"""

__all__: list[str] = []
