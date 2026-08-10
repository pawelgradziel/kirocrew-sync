"""SQLite unpack/pack.

unpack: live database -> one JSONL file per table, sorted by identity.
pack:   JSONL files -> live database, in foreign-key-safe order.

The databases run in WAL mode and are frequently left uncheckpointed, so the
main .db file can be a 4 KB shell with megabytes of real data sitting in the
-wal. Reading through SQLite (rather than copying files) is what makes this
correct; it is also why -wal and -shm never need to cross the wire.
"""

import json
import shutil
import sqlite3
from pathlib import Path

from . import paths as pathmod
from . import policy as pol
from .canon import BlobStore, dumps, row_identity


def connect_ro(path):
    """Read-only connection that still sees uncheckpointed WAL content."""
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def connect_rw(path):
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


class TableInfo:
    def __init__(self, name, sql, columns, pk_columns, blob_columns,
                 is_virtual, is_shadow):
        self.name = name
        self.sql = sql
        self.columns = columns
        self.pk_columns = pk_columns
        self.blob_columns = blob_columns
        self.is_virtual = is_virtual
        self.is_shadow = is_shadow


def inspect(conn):
    """Describe every table, flagging virtual tables and their shadow tables."""
    rows = conn.execute(
        "SELECT name, sql, type FROM sqlite_master "
        "WHERE type IN ('table') AND name NOT LIKE 'sqlite_stat%'").fetchall()

    virtual = {r["name"] for r in rows
               if (r["sql"] or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE")}

    tables = {}
    for r in rows:
        name = r["name"]
        is_virtual = name in virtual
        is_shadow = any(name.startswith(v + "_") for v in virtual)

        info = conn.execute("PRAGMA table_info(%s)" % _quote(name)).fetchall()
        columns = [c["name"] for c in info]
        pk_columns = [c["name"] for c in sorted(
            (c for c in info if c["pk"]), key=lambda c: c["pk"])]
        blob_columns = [c["name"] for c in info
                        if (c["type"] or "").upper().startswith("BLOB")]

        tables[name] = TableInfo(name, r["sql"], columns, pk_columns,
                                 blob_columns, is_virtual, is_shadow)
    return tables


def normalize_ddl(sql):
    """One statement per line, whitespace collapsed.

    Keeps _schema.sql line-diffable and makes the schema comparison in gates.py
    insensitive to formatting that SQLite may round-trip differently.
    """
    return " ".join((sql or "").split()).rstrip(";") + ";"


def schema_text(conn, db_name):
    """Canonical schema for the tables this database actually exports.

    unpack and the drift gate must agree exactly on which tables count, or the
    gate fires on every sync and users learn to pass --force.
    """
    tables = inspect(conn)
    lines = []
    for name in sorted(tables):
        t = tables[name]
        p = pol.for_table(db_name, t)
        if not pol.is_exported(p) or not t.sql:
            continue
        lines.append(normalize_ddl(t.sql))
    return sorted(lines)


def fts5_tables(conn):
    """Virtual tables backed by FTS5, which must be rebuilt rather than merged."""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
    return [r["name"] for r in rows
            if "USING fts5" in (r["sql"] or "").replace("using fts5", "USING fts5")]


def fk_order(conn, tables):
    """Topological order such that a table follows every table it references."""
    deps = {}
    for name in tables:
        refs = set()
        for fk in conn.execute("PRAGMA foreign_key_list(%s)" % _quote(name)):
            target = fk["table"]
            if target in tables and target != name:
                refs.add(target)
        deps[name] = refs

    ordered, placed = [], set()
    # Deterministic: alphabetical within each dependency level.
    remaining = sorted(deps)
    while remaining:
        ready = [n for n in remaining if deps[n] <= placed]
        if not ready:
            # A cycle. Break it deterministically; the FK check after pack will
            # report anything this leaves dangling.
            ready = [remaining[0]]
        for n in ready:
            ordered.append(n)
            placed.add(n)
            remaining.remove(n)
    return ordered


# --------------------------------------------------------------------------
# unpack
# --------------------------------------------------------------------------

def unpack_db(db_path, out_dir, db_name, blobs):
    """Export one database to canonical JSONL. Returns a per-table policy map."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(db_path)
    try:
        tables = inspect(conn)
        policies = {}
        resolved = {}
        mappings = pathmod.load_mappings()

        for name in sorted(tables):
            t = tables[name]
            p = pol.for_table(db_name, t)
            policies[name] = p
            if not pol.is_exported(p):
                continue

            identity = identity_columns(t, p)
            # A renumbered column holds a machine-local value; exporting it
            # would make every row look modified on the other machine.
            export_cols = [c for c in t.columns if c != p.renumber]
            # Filesystem paths are rewritten to a portable form here, so the
            # merge compares machine-independent values (see ADR 0001).
            path_col = pathmod.column_for(db_name, name)

            records = []
            for row in conn.execute("SELECT * FROM %s" % _quote(name)):
                record = {}
                for col in export_cols:
                    value = row[col]
                    if isinstance(value, bytes):
                        value = blobs.put(value)
                    elif col == path_col:
                        value = pathmod.encode(value, mappings)
                    record[col] = value
                records.append((row_identity(record, identity), record))

            records.sort(key=lambda pair: pair[0])
            path = out_dir / (name + ".jsonl")
            path.write_text(
                "".join(dumps(rec) + "\n" for _, rec in records), encoding="utf-8")
            resolved[name] = identity

        # Written as a separate file so a schema change surfaces as its own
        # conflict rather than as noise spread across every table.
        (out_dir / "_schema.sql").write_text(
            "\n".join(schema_text(conn, db_name)) + "\n", encoding="utf-8")

        # The merge driver reads this to learn each table's identity columns,
        # so it must carry the resolved list, not the declared one.
        policy_json = {}
        for name, p in policies.items():
            entry = p.to_json()
            entry["identity"] = resolved.get(name)
            policy_json[name] = entry
        (out_dir / "_policy.json").write_text(
            dumps(policy_json) + "\n", encoding="utf-8")

        return policies
    finally:
        conn.close()


def stale_jsonl(out_dir, policies):
    """JSONL files in the repo for tables that no longer exist or are excluded."""
    keep = {name + ".jsonl" for name, p in policies.items() if pol.is_exported(p)}
    return [p for p in Path(out_dir).glob("*.jsonl") if p.name not in keep]


# --------------------------------------------------------------------------
# pack
# --------------------------------------------------------------------------

def read_jsonl(path):
    rows = []
    if not Path(path).exists():
        return rows
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            if line.startswith("<<<<<<<") or line.startswith("=======") \
                    or line.startswith(">>>>>>>") or line.startswith("|||||||"):
                raise ValueError(
                    "%s:%d contains unresolved conflict markers" % (path, lineno))
            rows.append(json.loads(line))
    return rows


def identity_columns(table_info, table_policy):
    """Columns that identify a row, consistently on both the DB and JSONL side.

    BLOB columns are excluded because the two sides represent them differently
    (raw bytes vs. a content-addressed reference), and the renumbered column is
    excluded because its value is machine-local by definition.
    """
    cols = list(table_policy.identity) if table_policy.identity \
        else (table_info.pk_columns or table_info.columns)
    cols = [c for c in cols if c not in table_info.blob_columns]
    if table_policy.renumber:
        cols = [c for c in cols if c != table_policy.renumber]
    return cols or list(table_info.columns)


def pack_db(db_path, in_dir, db_name, blobs, dry_run=False, log=print):
    """Apply canonical JSONL to a live database inside one transaction."""
    in_dir = Path(in_dir)
    conn = connect_rw(db_path)
    stats = {"applied": 0, "deleted": 0, "orphans": [], "path_warnings": []}
    mappings = pathmod.load_mappings()
    try:
        tables = inspect(conn)
        syncable = {}
        for name, t in tables.items():
            p = pol.for_table(db_name, t)
            if pol.is_exported(p):
                syncable[name] = (t, p)

        order = [n for n in fk_order(conn, syncable) if n in syncable]

        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")

        # Deletes run children-first so a parent never disappears while a row
        # still points at it.
        for name in reversed(order):
            t, p = syncable[name]
            path = in_dir / (name + ".jsonl")
            if not path.exists():
                continue
            ident = identity_columns(t, p)
            wanted = {row_identity(r, ident) for r in read_jsonl(path)}

            for row, rowid in _iter_with_handle(conn, t):
                if row_identity(row, ident) not in wanted:
                    _delete_row(conn, t, row, rowid)
                    stats["deleted"] += 1

        # Inserts run parents-first.
        for name in order:
            t, p = syncable[name]
            path = in_dir / (name + ".jsonl")
            if not path.exists():
                continue
            rows = read_jsonl(path)
            if p.renumber:
                rows = _renumber(rows, p.renumber, identity_columns(t, p))

            cols = ",".join(_quote(c) for c in t.columns)
            marks = ",".join("?" for _ in t.columns)
            statement = "INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (
                _quote(name), cols, marks)
            # Portable paths become this machine's absolute paths again; the
            # live database must never hold a "~" (see ADR 0001).
            path_col = pathmod.column_for(db_name, name)

            for record in rows:
                values = []
                for col in t.columns:
                    value = record.get(col)
                    if BlobStore.is_ref(value):
                        value = blobs.get(value)
                    elif col == path_col:
                        value, warning = pathmod.decode(value, mappings)
                        if warning:
                            stats["path_warnings"].append(
                                "%s.%s: %s" % (name, col, warning))
                    values.append(value)
                conn.execute(statement, values)
                stats["applied"] += 1

            if p.renumber and rows:
                # sqlite_sequence is not synced, so realign the AUTOINCREMENT
                # counter with the ids we just assigned. Without this the next
                # local insert reuses an id that already exists.
                high = max(r[p.renumber] for r in rows)
                # sqlite_sequence has no unique index, so upsert is unavailable.
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='sqlite_sequence'").fetchone():
                    conn.execute("DELETE FROM sqlite_sequence WHERE name=?", [name])
                    conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)",
                                 [name, high])

        if dry_run:
            conn.execute("ROLLBACK")
            log("  (dry run - rolled back)")
            return stats

        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys=ON")

        for row in conn.execute("PRAGMA foreign_key_check").fetchall():
            stats["orphans"].append({"table": row[0], "rowid": row[1],
                                     "parent": row[2]})

        for name in fts5_tables(conn):
            try:
                conn.execute("INSERT INTO %s(%s) VALUES('rebuild')"
                             % (_quote(name), _quote(name)))
                conn.commit()
                log("  rebuilt FTS index: %s" % name)
            except sqlite3.Error as exc:
                log("  WARN: could not rebuild %s: %s" % (name, exc))

        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
        return stats
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _iter_with_handle(conn, table_info):
    """Yield (row-without-blobs, rowid) for every row, rowid None if absent."""
    non_blob = [c for c in table_info.columns if c not in table_info.blob_columns]
    try:
        cursor = conn.execute(
            "SELECT rowid AS _kc_rowid_, %s FROM %s"
            % (",".join(_quote(c) for c in non_blob), _quote(table_info.name)))
        has_rowid = True
    except sqlite3.OperationalError:
        # WITHOUT ROWID table; it is guaranteed to have a primary key.
        cursor = conn.execute(
            "SELECT %s FROM %s"
            % (",".join(_quote(c) for c in non_blob), _quote(table_info.name)))
        has_rowid = False

    for row in cursor.fetchall():
        record = {c: row[c] for c in non_blob}
        yield record, (row["_kc_rowid_"] if has_rowid else None)


def _delete_row(conn, table_info, record, rowid):
    if rowid is not None:
        conn.execute("DELETE FROM %s WHERE rowid=?" % _quote(table_info.name),
                     [rowid])
        return
    keys = table_info.pk_columns or [c for c in table_info.columns
                                     if c not in table_info.blob_columns]
    where = " AND ".join("%s IS ?" % _quote(c) for c in keys)
    conn.execute("DELETE FROM %s WHERE %s" % (_quote(table_info.name), where),
                 [record.get(c) for c in keys])


def _renumber(rows, column, identity):
    """Reassign an AUTOINCREMENT column after a cross-machine union.

    Ordering is by the natural identity so both machines land on the same
    numbering and the next sync sees no change.
    """
    keyed = sorted(rows, key=lambda r: row_identity(r, list(identity)))
    for index, record in enumerate(keyed, 1):
        record[column] = index
    return keyed


def backup(db_path, dest_dir):
    """Copy a database (WAL included) before mutating it."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    src = Path(db_path)
    if not src.exists():
        return None
    target = dest_dir / src.name
    # sqlite3's backup API produces a consistent copy even with a hot WAL.
    source = connect_ro(src)
    try:
        out = sqlite3.connect(str(target))
        with out:
            source.backup(out)
        out.close()
    finally:
        source.close()
    return target


def restore(backup_path, db_path):
    shutil.copy2(backup_path, db_path)
    for suffix in ("-wal", "-shm"):
        stale = Path(str(db_path) + suffix)
        if stale.exists():
            stale.unlink()
