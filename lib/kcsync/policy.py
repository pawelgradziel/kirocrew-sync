"""Per-table merge policy.

Policies are inferred from the schema so that tables KiroCrew adds later still
sync sensibly, with explicit overrides for the tables whose semantics the
schema alone does not reveal (append-only logs, machine-local scratch state).
"""

from dataclasses import dataclass, field, asdict

# Merge modes
SKIP = "skip"      # derived data - never exported, rebuilt locally after pack
LOCAL = "local"    # machine-local state - never exported, never overwritten
LWW = "lww"        # mutable rows - last writer wins on ts_col
UNION = "union"    # immutable rows - set union, keyed by identity


@dataclass
class TablePolicy:
    mode: str
    ts_col: str = None            # timestamp column used to break LWW ties
    tombstone_col: str = None     # soft-delete flag, if the table has one
    identity: tuple = None        # identity columns; None means "use the PK"
    renumber: str = None          # AUTOINCREMENT column reassigned at pack time
    note: str = ""

    def to_json(self):
        d = asdict(self)
        if d["identity"] is not None:
            d["identity"] = list(d["identity"])
        return d

    @staticmethod
    def from_json(d):
        d = dict(d)
        if d.get("identity") is not None:
            d["identity"] = tuple(d["identity"])
        return TablePolicy(**d)


# Databases that carry syncable state, keyed by logical name.
DATABASES = {
    "memory": "memory.db",
    "knowledge": "workspace/knowledge/knowledge.db",
}

# Databases that are pure derived indexes. Never synced; rebuilt by KiroCrew.
DERIVED_DATABASES = [
    "memory_index.db",
]

OVERRIDES = {
    "memory": {
        "semantic_memory": TablePolicy(
            LWW, ts_col="updated_at", tombstone_col="is_deleted"),
        # Episodic memories are written once; last_accessed_at is read state,
        # not modification state, so it must not drive LWW.
        "episodic_memories": TablePolicy(
            LWW, ts_col="created_at", tombstone_col="is_deleted"),
        # AUTOINCREMENT id is machine-relative: id 7 on two machines refers to
        # two different events. Merge on the natural key, renumber on pack.
        "memory_events": TablePolicy(
            UNION,
            identity=("created_at", "event_type", "memory_type", "memory_key", "source"),
            renumber="id",
            note="append-only event log; ids are machine-local"),
        "memory_meta": TablePolicy(LWW, ts_col="updated_at"),
        "schema_version": TablePolicy(UNION),
        "sqlite_sequence": TablePolicy(SKIP, note="managed by SQLite"),
    },
    "knowledge": {
        "sources": TablePolicy(LWW, ts_col="updated_at"),
        "items": TablePolicy(LWW, ts_col="updated_at"),
        "entities": TablePolicy(LWW, ts_col="updated_at"),
        "entity_relations": TablePolicy(UNION),
        "mentions": TablePolicy(UNION),
        "source_locations": TablePolicy(UNION),
        "artifact_item_state": TablePolicy(LWW, ts_col="updated_at"),
        "dismissed_auto_sources": TablePolicy(UNION),
        # Per-file ingest state. It holds absolute paths, but those are made
        # portable at the sync boundary (ADR 0001), and item_ids links each
        # scanned file to the items built from it. Dropping it would make the
        # receiving machine re-ingest and re-embed a folder it already has.
        "folder_file_state": TablePolicy(LWW, ts_col="last_seen"),
        "ingestion_jobs": TablePolicy(LOCAL, note="transient job state"),
    },
}

# Columns that, when present, are good LWW discriminators, in preference order.
_TS_CANDIDATES = ("updated_at", "modified_at", "created_at")


def infer(table, columns, pk_columns, is_virtual, is_shadow):
    """Best-effort policy for a table with no explicit override."""
    if is_virtual or is_shadow:
        return TablePolicy(SKIP, note="FTS/virtual table; rebuilt after pack")
    for candidate in _TS_CANDIDATES:
        if candidate in columns:
            tomb = "is_deleted" if "is_deleted" in columns else None
            return TablePolicy(LWW, ts_col=candidate, tombstone_col=tomb,
                               note="inferred from schema")
    if pk_columns:
        return TablePolicy(UNION, note="inferred: keyed, no timestamp")
    # No primary key: treat the whole row as its own identity, which reduces to
    # set-union of distinct rows.
    return TablePolicy(UNION, identity=tuple(columns),
                       note="inferred: no primary key, row-hash identity")


def for_table(db_name, table, columns, pk_columns, is_virtual, is_shadow):
    override = OVERRIDES.get(db_name, {}).get(table)
    if override is not None:
        return override
    return infer(table, columns, pk_columns, is_virtual, is_shadow)
