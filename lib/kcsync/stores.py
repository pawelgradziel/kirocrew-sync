"""Per-member memory stores: discovery, name validation, creation.

KiroCrew 0.7 keeps one directory per named memory store under
``<data home>/memory_stores/`` (upstream ``memory_stores.py``). Two shapes
share that tree:

    memory_stores/
      .member-api-key            host-local (historical credential)
      .member-backups/           host-local (rolling backups, restore
                                 journals, namespace and store-use locks)
      .execution-logs/           host-local
      <name>/                    one store
        memory.db                the memory: crew-lineage SQLite, synced row
                                 by row as logical database memory_stores/<name>
        memory/preferences.md    manual documents, synced as files
        memory/projects.md
        memory/history/*.md      named V1 store only
        lessons.jsonl            named V1 store only (fallback lesson file)
        member-memory.json       legacy ownership manifest (read, never written)
        memory_index.db          named V1 store only: derived FTS, never synced
        backups/                 named V1 store only: host-local
        memory.db-wal/-shm       never synced; unpack reads through SQLite

A V2 member store's name is ``member-<member id>-<uuid4 hex>``, allocated once
on the machine that created the member. It is not machine-specific: the store
id is recorded in the database's own ``member_database`` row and in
config.json, both of which travel, so the other machine gets the same name.

The host-local set is upstream's ``is_host_local_store_state``; files.DENY
spells the same entries. Nothing here is discovered in team scope.
"""

import json
import os
import re
import sqlite3
import stat
from pathlib import Path

from . import policy as pol

STORES_DIR = "memory_stores"
STORE_DB_FILE = "memory.db"
DDL_FILE = "_ddl.json"

# Upstream memory_stores.memory_store_name_defect, restated. A name is one
# lowercase path segment of letters, digits and inner hyphens: no '/', '\\',
# '.' or whitespace can pass, so a validated name cannot leave the stores
# root. "default" is the global store, which lives at the data-home root and
# is never a directory under memory_stores/.
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")
_WINDOWS_RESERVED = frozenset(
    {"con", "nul", "aux", "prn"}
    | {"com%d" % i for i in range(1, 10)}
    | {"lpt%d" % i for i in range(1, 10)})
DEFAULT_STORE = "default"


def is_valid_name(name):
    return (isinstance(name, str)
            and bool(_NAME_RE.match(name))
            and name not in _WINDOWS_RESERVED
            and name != DEFAULT_STORE)


def db_name(store):
    """Logical database name for *store*."""
    return pol.STORE_DB_PREFIX + store


def rel_path(store):
    """Data-home-relative path of *store*'s database."""
    return "%s/%s/%s" % (STORES_DIR, store, STORE_DB_FILE)


def store_of(logical):
    """The store name inside a `memory_stores/<name>` logical name, or None."""
    if not pol.is_store_db(logical):
        return None
    name = logical[len(pol.STORE_DB_PREFIX):]
    return name if is_valid_name(name) else None


def store_dir(kirocrew_dir, store):
    """*store*'s directory, or None when it would not be that store's own.

    The same identity test upstream's _named_store_dir applies: the resolved
    path must be the composed one. A symlink redirecting memory_stores/acme to
    memory_stores/finance still resolves inside the root, so a containment
    check would accept it and merge one member's memory into another's.
    """
    if not is_valid_name(store):
        return None
    root = Path(kirocrew_dir) / STORES_DIR
    try:
        expected = root.resolve() / store
        if (root / store).resolve() != expected:
            return None
    except OSError:
        return None
    return root / store


def _private_regular_file(path):
    """Regular file, not a symlink, exactly one name (upstream's rule)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def writable_here(kirocrew_dir, store):
    """True when pack may write *store*'s database on this machine.

    Either the store is absent (pack creates it) or its directory and
    database pass the same identity checks discovery applies.
    """
    directory = store_dir(kirocrew_dir, store)
    if directory is None:
        return False
    db_path = directory / STORE_DB_FILE
    if not os.path.lexists(db_path):
        return True
    return _private_regular_file(db_path)


def local_stores(kirocrew_dir, log=None):
    """Names of the stores on this machine that are safe to read."""
    root = Path(kirocrew_dir) / STORES_DIR
    if not root.is_dir():
        return []
    found = []
    for entry in sorted(root.iterdir()):
        if not (entry / STORE_DB_FILE).exists() and not entry.is_symlink():
            continue           # host-local dirs, stores with no database yet
        if not is_valid_name(entry.name):
            if log and not entry.name.startswith("."):
                log("  WARN: skipping memory store with an invalid name: %r"
                    % entry.name)
            continue
        directory = store_dir(kirocrew_dir, entry.name)
        if directory is None or not _private_regular_file(directory / STORE_DB_FILE):
            if log:
                log("  WARN: skipping memory store %s: its directory or "
                    "database is a link" % entry.name)
            continue
        found.append(entry.name)
    return found


def repo_stores(repo_dir):
    """Names of the stores present in the sync repo's db/ tree."""
    root = Path(repo_dir) / "db" / STORES_DIR
    if not root.is_dir():
        return []
    return [p.name for p in sorted(root.iterdir())
            if p.is_dir() and is_valid_name(p.name)
            and ((p / "_schema.sql").exists() or (p / DDL_FILE).exists())]


def databases(kirocrew_dir, repo_dir=None, scope=pol.PERSONAL, log=None):
    """{logical name: data-home-relative path} for every syncable database.

    The fixed DATABASES first, then one entry per store: those on this machine,
    plus (when *repo_dir* is given) those that arrived in the sync repo from
    another machine. Team scope returns the fixed databases only -- member
    memory is private, and a store name alone discloses a member's id.
    """
    result = dict(pol.DATABASES)
    if scope != pol.PERSONAL:
        return result
    names = set(local_stores(kirocrew_dir, log))
    if repo_dir is not None:
        names.update(repo_stores(repo_dir))
    for name in sorted(names):
        result[db_name(name)] = rel_path(name)
    return result


# --------------------------------------------------------------------------
# Creating a store that exists only on another machine
# --------------------------------------------------------------------------

def full_ddl(conn):
    """Every CREATE statement needed to recreate this database's structure.

    _schema.sql lists only the exported tables, which is what the drift gate
    compares. Recreating the store also needs what is never exported: the
    FTS5 table (which recreates its own shadow tables), the views KiroCrew
    reads through, and the indexes. Kept verbatim -- sqlite_master already
    normalizes CREATE text, so re-running it reproduces identical text.
    """
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").fetchall()
    virtual = {r[1] for r in rows if r[0] == "table"
               and r[2].lstrip().upper().startswith("CREATE VIRTUAL TABLE")}
    order = {"table": 0, "index": 1, "view": 2, "trigger": 3}
    statements = []
    for kind, name, sql in rows:
        if kind == "table" and any(name.startswith(v + "_") for v in virtual):
            continue           # FTS5 shadow table; created by its parent
        statements.append((order.get(kind, 9), name, sql))
    return [sql for _, _, sql in sorted(statements)]


def write_ddl(db_path, out_dir):
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=30)
    try:
        ddl = full_ddl(conn)
    finally:
        conn.close()
    (Path(out_dir) / DDL_FILE).write_text(
        json.dumps(ddl, indent=2) + "\n", encoding="utf-8")


def create_from_repo(kirocrew_dir, store, source_dir):
    """Create *store*'s empty database from the repo's _ddl.json.

    Returns the database path. Refuses rather than overwrites: an existing
    file is never touched here. The directory and file are owner-only, as
    upstream creates them. Rows are then applied by the ordinary pack.
    """
    ddl_path = Path(source_dir) / DDL_FILE
    ddl = json.loads(ddl_path.read_text(encoding="utf-8"))
    if not isinstance(ddl, list) or not all(isinstance(s, str) for s in ddl):
        raise ValueError("%s is not a list of statements" % ddl_path)
    for statement in ddl:
        head = " ".join(statement.split()[:2]).upper()
        if head not in ("CREATE TABLE", "CREATE VIRTUAL", "CREATE INDEX",
                        "CREATE UNIQUE", "CREATE VIEW", "CREATE TRIGGER"):
            raise ValueError("%s holds a statement that is not a CREATE"
                             % ddl_path)

    root = Path(kirocrew_dir) / STORES_DIR
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = store_dir(kirocrew_dir, store)
    if directory is None:
        raise ValueError("memory store %r does not resolve to its own "
                         "directory" % store)
    directory.mkdir(mode=0o700, exist_ok=True)
    db_path = directory / STORE_DB_FILE
    # Exclusive create: never clobber a database that appeared meanwhile.
    fd = os.open(db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            for statement in ddl:
                conn.execute(statement)
    except Exception:
        conn.close()
        remove_created(db_path)
        raise
    conn.close()
    return db_path


def remove_created(db_path):
    """Undo create_from_repo after a failed pack."""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(str(db_path) + suffix)
        except OSError:
            pass


def rebuild_member_fts(db_path, log=print):
    """Repopulate a member store's memory_fts from the rows it indexes.

    memory_fts is an ordinary (not external-content) FTS5 table, so SQLite's
    'rebuild' only re-indexes whatever text it already holds and never sees a
    row that arrived by sync. This is upstream's rebuild_memory_index
    (vector_memory.py) verbatim: live memory_items rows plus every history day.
    """
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"memory_fts", "memory_items", "memory_history"} <= names:
            return None
        with conn:
            conn.execute("DELETE FROM memory_fts")
            conn.execute(
                "INSERT INTO memory_fts(path,content) SELECT id,"
                "COALESCE(key,'')||' '||text FROM memory_items WHERE is_deleted=0")
            conn.execute(
                "INSERT INTO memory_fts(path,content) SELECT 'history:'||day,"
                "content FROM memory_history")
        count = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return count
    except sqlite3.Error as exc:
        log("  WARN: could not rebuild memory_fts in %s: %s" % (db_path, exc))
        return None
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Plain files under memory_stores/
# --------------------------------------------------------------------------

def file_store_ok(rel_path, kirocrew_dir=None):
    """True unless *rel_path* is under memory_stores/ in an unusable store.

    For paths outside memory_stores/ this has no opinion. Inside, the store
    segment must be a valid store name, and, when *kirocrew_dir* is given, the
    store's directory must be that store's own (no redirecting symlink).
    """
    parts = Path(rel_path).parts
    if not parts or parts[0] != STORES_DIR:
        return True
    if len(parts) < 3 or not is_valid_name(parts[1]):
        return False
    if kirocrew_dir is not None:
        return store_dir(kirocrew_dir, parts[1]) is not None
    return True


def make_private(kirocrew_dir, rel_path):
    """Owner-only permissions for a file written under memory_stores/."""
    parts = Path(rel_path).parts
    if not parts or parts[0] != STORES_DIR:
        return
    root = Path(kirocrew_dir)
    try:
        os.chmod(root / STORES_DIR, 0o700)
        path = root
        for part in parts[:-1]:
            path = path / part
            os.chmod(path, 0o700)
        os.chmod(root / rel_path, 0o600)
    except OSError:
        pass
