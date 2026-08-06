#!/usr/bin/env python3
"""Translate KiroCrew knowledge-base paths between machine-local and portable form.

The knowledge database stores filesystem paths verbatim: a folder source added on
one machine keeps that machine's absolute path forever. Sync the database to a
second machine with a different layout and every folder source breaks.

This module fixes that at the sync boundary rather than in the database KiroCrew
actually reads:

    push:  local absolute  --encode-->  portable   (rewrites the BUNDLE copy)
    pull:  portable        --decode-->  local absolute (rewrites the BUNDLE copy)

The live knowledge.db always holds real absolute paths. That is deliberate --
KiroCrew resolves source URIs with a bare ``Path(uri)`` (see
``knowledge/folder_watcher.py`` and ``connectors/local_folder.py``), which does
NOT expand ``~``. Storing tilde paths in the live database would break every
folder source on every machine, including the one that created it.

Portable form:

    ~/code/repo          path under $HOME
    ${CODE}/repo         path under a user-declared mapping (see path_map.conf)
    /opt/shared/docs     anything else -- left absolute, still machine-specific

Only these columns carry filesystem paths; everything else is left alone:

    sources.uri                     the folder root
    folder_file_state.file_path     per-file ingest state under that root
    dismissed_auto_sources.uri      tombstones for auto-discovered project dirs

Both directions are idempotent and safe to run on databases written by older
versions of this tool: encoding an already-portable path is a no-op, and
decoding a plain absolute path is a no-op.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

# ``${NAME}`` or ``${NAME}/rest`` -- the mapped form of a portable path.
_VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}(?:/(.*))?$")

# ``artifact://``, ``https://`` and friends share the uri column with real
# paths and must never be touched.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")

# ``NAME = /local/path`` lines in the mapping file.
_MAPPING_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")

# (table, path column, columns identifying the row) -- update targets, in the
# order they are rewritten. Tables or columns missing from a given schema
# version are skipped rather than treated as an error.
TARGETS = (
    ("sources", "uri", ("id",)),
    ("folder_file_state", "file_path", ("source_id", "file_path")),
    ("dismissed_auto_sources", "uri", ("uri",)),
)


class PathMapError(Exception):
    """The mapping file could not be used as written."""


# --------------------------------------------------------------------------
# Mapping file
# --------------------------------------------------------------------------

def load_mappings(map_file: str | None) -> list[tuple[str, str]]:
    """Read ``NAME = /local/path`` declarations, longest local path first.

    Longest-first ordering is what makes nested mappings behave: with both
    ``CODE = ~/code`` and ``WORK = ~/code/work`` declared, a path under
    ``~/code/work`` encodes as ``${WORK}/...``, the more specific of the two.

    Returns an empty list when no file exists -- mappings are optional, and
    without them everything under $HOME still encodes as ``~/...``.
    """
    if not map_file:
        return []
    path = Path(map_file).expanduser()
    if not path.is_file():
        return []

    mappings: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _MAPPING_RE.match(line)
        if not m:
            raise PathMapError(f"{path}:{lineno}: expected 'NAME = /local/path', got: {raw.strip()}")
        name, value = m.group(1), m.group(2)
        if name == "HOME":
            raise PathMapError(f"{path}:{lineno}: HOME is built in -- use ~ instead of a mapping")
        if name in mappings:
            raise PathMapError(f"{path}:{lineno}: '{name}' declared twice")
        local = str(Path(os.path.expandvars(value)).expanduser())
        if not local.startswith("/"):
            raise PathMapError(f"{path}:{lineno}: '{name}' must map to an absolute path, got: {value}")
        mappings[name] = local.rstrip("/") or "/"

    return sorted(mappings.items(), key=lambda kv: len(kv[1]), reverse=True)


# --------------------------------------------------------------------------
# Path translation
# --------------------------------------------------------------------------

def _relative_to(path: str, base: str) -> str | None:
    """Return *path* relative to *base*, or None when it is not underneath.

    Compares whole path components, so ``/home/pawel2/x`` is not treated as
    living under ``/home/pawel``.
    """
    if path == base:
        return ""
    prefix = base if base.endswith("/") else base + "/"
    return path[len(prefix):] if path.startswith(prefix) else None


def is_portable(value: str) -> bool:
    """True when *value* is already in portable form."""
    return value.startswith("~") or bool(_VAR_RE.match(value))


def is_local_path(value: str) -> bool:
    """True when *value* looks like a machine-local filesystem path.

    Guards the URI columns, which also hold non-path values such as
    ``artifact://`` or ``https://...``; those must pass through untouched.
    """
    return value.startswith("/")


def to_portable(value: str, mappings: list[tuple[str, str]], home: str) -> str:
    """Rewrite an absolute local path into portable form.

    Anything already portable, non-absolute, or matching no prefix is returned
    unchanged -- an absolute path outside $HOME and outside every mapping stays
    absolute, which is the honest result: nothing here can make it portable.

    Symlinks are deliberately NOT resolved. The logical path the user typed
    (``~/projects/groover``) travels between machines far better than whatever
    it happens to point at on this one (``/mnt/storage/repos/groover``).
    """
    if is_portable(value) or not is_local_path(value):
        return value

    for name, local in mappings:
        rel = _relative_to(value, local)
        if rel is not None:
            return f"${{{name}}}/{rel}" if rel else f"${{{name}}}"

    rel = _relative_to(value, home)
    if rel is not None:
        return f"~/{rel}" if rel else "~"

    return value


def to_local(value: str, mappings: list[tuple[str, str]], home: str) -> tuple[str, str | None]:
    """Rewrite a portable path into this machine's absolute path.

    Returns ``(path, unresolved_var)``. When a ``${NAME}`` has no mapping on
    this machine the original value is returned untouched alongside the
    variable name: leaving a path visibly unresolved is far better than
    silently pointing a knowledge source at the wrong directory.
    """
    m = _VAR_RE.match(value)
    if m:
        name, rest = m.group(1), m.group(2) or ""
        if name == "HOME":
            base = home
        else:
            base = next((local for var, local in mappings if var == name), None)
            if base is None:
                return value, name
        return (f"{base.rstrip('/')}/{rest}" if rest else base), None

    if value == "~":
        return home, None
    if value.startswith("~/"):
        return f"{home.rstrip('/')}/{value[2:]}", None

    return value, None


# --------------------------------------------------------------------------
# Database rewriting
# --------------------------------------------------------------------------

def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    """Column names of *table*, empty when the table does not exist.

    Schema tolerance matters here: this tool runs against databases written by
    whatever KiroCrew version the other machine had.
    """
    try:
        return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.DatabaseError:
        return set()


class Rewriter:
    """Applies one translation direction across every path-bearing column."""

    def __init__(self, mappings: list[tuple[str, str]], home: str, *, dry_run: bool = False):
        self.mappings = mappings
        self.home = home.rstrip("/") or "/"
        self.dry_run = dry_run
        self.changed = 0
        self.skipped_collisions: list[str] = []
        self.relative_paths: list[str] = []
        self.unresolved: dict[str, list[str]] = {}
        self.missing: list[tuple[str, str]] = []

    def _translate(self, value: str, direction: str) -> str | None:
        """Return the new value for *value*, or None to leave the row alone."""
        if direction == "encode":
            if is_portable(value) or _SCHEME_RE.match(value):
                return None
            if not value.startswith("/"):
                # Relative URIs ("./docs") resolve against the CWD of whatever
                # process added them, which is not knowable here -- resolving
                # against this script's CWD would invent a wrong path. Report
                # and leave untouched.
                self.relative_paths.append(value)
                return None
            new = to_portable(value, self.mappings, self.home)
        else:
            new, unresolved = to_local(value, self.mappings, self.home)
            if unresolved:
                self.unresolved.setdefault(unresolved, []).append(value)
                return None
        return new if new != value else None

    def run(self, db: sqlite3.Connection, direction: str) -> None:
        """Rewrite every target column in a single transaction."""
        for table, column, key_columns in TARGETS:
            cols = _columns(db, table)
            if not cols or column not in cols or not set(key_columns) <= cols:
                continue

            select_cols = ", ".join(dict.fromkeys((column, *key_columns)))
            rows = db.execute(f"SELECT {select_cols} FROM {table}").fetchall()  # noqa: S608 - names are literals above
            where = " AND ".join(f"{c} = ?" for c in key_columns)

            for row in rows:
                value = row[column]
                if not isinstance(value, str) or not value:
                    continue
                new = self._translate(value, direction)
                if new is None:
                    continue
                if self.dry_run:
                    self.changed += 1
                    continue
                try:
                    db.execute(
                        f"UPDATE {table} SET {column} = ? WHERE {where}",  # noqa: S608 - names are literals above
                        (new, *(row[c] for c in key_columns)),
                    )
                except sqlite3.IntegrityError:
                    # sources.uri is UNIQUE and folder_file_state has a
                    # (source_id, file_path) primary key: two rows that differ
                    # only in spelling collide once both are normalized.
                    # Dropping or merging one would take entities, items and
                    # ingest state with it, so the row is left as it was and
                    # reported instead.
                    self.skipped_collisions.append(f"{table}.{column}: {value} -> {new}")
                    continue
                self.changed += 1

        if direction == "decode":
            self._collect_missing(db)

    def _collect_missing(self, db: sqlite3.Connection) -> None:
        """Note source folders that decoded cleanly but are not on this machine.

        Runs on the connection that just did the rewriting: reopening the file
        would recreate the write-ahead log that the checkpoint is about to
        remove, and put it back in the bundle.
        """
        if "uri" not in _columns(db, "sources"):
            return
        for row in db.execute("SELECT name, uri FROM sources"):
            uri = row["uri"]
            if is_local_path(uri) and not Path(uri).is_dir():
                self.missing.append((row["name"], uri))


def open_db(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the database at *path*, with rows addressable by column name."""
    uri = f"file:{path}?mode=ro" if read_only else f"file:{path}"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    return db


def rewrite(db_path: Path, direction: str, mappings: list[tuple[str, str]], *,
            dry_run: bool = False) -> Rewriter:
    """Translate *db_path* in place, in a single transaction.

    Also folds any write-ahead log into the main file: a bundled database
    arrives as knowledge.db plus knowledge.db-wal, and the WAL can hold the
    bulk of the data. Checkpointing collapses the pair into one self-contained
    file, which is what should travel in a sync bundle.
    """
    rewriter = Rewriter(mappings, str(Path.home()), dry_run=dry_run)
    db = open_db(db_path)
    try:
        with db:
            rewriter.run(db, direction)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
    return rewriter


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _report_rewriter(r: Rewriter, direction: str) -> None:
    verb = "portable" if direction == "encode" else "local"
    print(f"  {r.changed} path(s) rewritten to {verb} form")

    for value in dict.fromkeys(r.relative_paths):
        print(f"  ⚠ relative path left as-is (cannot be resolved reliably): {value}")

    for name, values in r.unresolved.items():
        print(f"  ⚠ no mapping for ${{{name}}} on this machine -- {len(values)} path(s) left unresolved:")
        for value in values[:5]:
            print(f"      {value}")
        if len(values) > 5:
            print(f"      ... and {len(values) - 5} more")
        print(f"  → add a line to the path map:  {name} = /your/local/path")

    for collision in r.skipped_collisions:
        print(f"  ⚠ duplicate after normalization, row left unchanged: {collision}")


def cmd_encode(args: argparse.Namespace, mappings: list[tuple[str, str]]) -> int:
    r = rewrite(args.database, "encode", mappings, dry_run=args.dry_run)
    _report_rewriter(r, "encode")
    return 0


def cmd_decode(args: argparse.Namespace, mappings: list[tuple[str, str]]) -> int:
    r = rewrite(args.database, "decode", mappings, dry_run=args.dry_run)
    _report_rewriter(r, "decode")

    # A path that decoded cleanly can still be missing on this machine -- that
    # is exactly the layout difference the user needs to hear about.
    if r.missing:
        print(f"  ⚠ {len(r.missing)} source path(s) do not exist on this machine:")
        for name, uri in r.missing:
            print(f"      {name}: {uri}")
        print("  → fix the layout, or map it: see path_map.conf.example")
    return 0


def cmd_report(args: argparse.Namespace, mappings: list[tuple[str, str]]) -> int:
    """Show how each source path stands on this machine. Read-only."""
    db = open_db(args.database, read_only=True)
    try:
        if "uri" not in _columns(db, "sources"):
            print("  no sources table in this database")
            return 0
        rows = db.execute("SELECT name, source_type, uri FROM sources ORDER BY source_type, name").fetchall()
    finally:
        db.close()

    paths = [r for r in rows if is_local_path(r["uri"]) or is_portable(r["uri"])]
    print(f"  {len(rows)} source(s), {len(paths)} path-based")
    if not paths:
        return 0

    home = str(Path.home())
    portable_count = unportable = broken = 0

    for row in paths:
        uri = row["uri"]
        local, unresolved = to_local(uri, mappings, home)
        portable = to_portable(local, mappings, home)
        exists = Path(local).is_dir() if is_local_path(local) else False

        # Resolving here and surviving a sync are separate questions, and a
        # source can fail both. Report each one it fails.
        notes = []
        if unresolved:
            notes.append(f"unmapped ${{{unresolved}}} -- cannot resolve on this machine")
            broken += 1
        else:
            if not exists:
                notes.append("path not found on this machine")
                broken += 1
            if is_portable(portable):
                notes.append(f"syncs as {portable}")
                portable_count += 1
            else:
                notes.append("outside $HOME and unmapped -- will not survive sync")
                unportable += 1

        if unresolved or not exists:
            mark = "✗"
        elif is_portable(portable):
            mark = "✓"
        else:
            mark = "⚠"

        print(f"  {mark} {row['name']} [{row['source_type']}]")
        print(f"      {local}")
        for note in notes:
            print(f"      {note}")

    print()
    print(f"  portable: {portable_count}   "
          f"will not survive sync: {unportable}   "
          f"missing on this machine: {broken}")
    if unportable:
        print("  → give machine-specific paths a name in the path map so they travel:")
        print("      see path_map.conf.example")
    return 0 if broken == 0 else 1


COMMANDS = {"encode": cmd_encode, "decode": cmd_decode, "report": cmd_report}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="portable_paths.py",
        description="Translate KiroCrew knowledge paths between local and portable form.")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("database", type=Path, help="path to knowledge.db")
    parser.add_argument("--map-file", default=os.environ.get("KIROCREW_PATH_MAP"),
                        help="path mapping file (default: $KIROCREW_PATH_MAP)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args(argv)

    if not args.database.is_file():
        print(f"  no knowledge database at {args.database} -- nothing to do")
        return 0

    try:
        mappings = load_mappings(args.map_file)
    except (PathMapError, OSError) as exc:
        print(f"  ✗ path map unusable: {exc}", file=sys.stderr)
        return 2

    try:
        return COMMANDS[args.command](args, mappings)
    except sqlite3.DatabaseError as exc:
        print(f"  ✗ database error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
