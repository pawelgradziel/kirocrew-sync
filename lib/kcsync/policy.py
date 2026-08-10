"""Per-table merge policy.

Policies are inferred from the schema so that tables KiroCrew adds later still
sync sensibly, with explicit overrides for the tables whose semantics the
schema alone does not reveal (append-only logs, machine-local scratch state).

Sync scope
----------
`personal` is one person's own machines: everything syncable travels. `team` is
several people sharing one library, and there the rule inverts -- a table
travels only if it is explicitly marked ``shared``. Opt-in, not opt-out: a
table KiroCrew adds in a later version stays on the machine until someone has
decided it is safe to publish to colleagues, which is the only direction in
which a wrong guess is harmless.
"""

from dataclasses import dataclass, asdict

# Merge modes
SKIP = "skip"      # derived data - never exported, rebuilt locally after pack
LOCAL = "local"    # machine-local state - never exported, never overwritten
LWW = "lww"        # mutable rows - last writer wins on ts_col
UNION = "union"    # immutable rows - set union, keyed by identity

# Sync scopes
PERSONAL = "personal"
TEAM = "team"
SCOPES = (PERSONAL, TEAM)


@dataclass(frozen=True)
class TablePolicy:
    mode: str
    ts_col: str = None            # timestamp column used to break LWW ties
    tombstone_col: str = None     # soft-delete flag, if the table has one
    identity: tuple = None        # identity columns; None means "use the PK"
    renumber: str = None          # AUTOINCREMENT column reassigned at pack time
    shared: bool = False          # also travels in team scope
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
        # Repos written before team scope existed have no "shared" key.
        d.setdefault("shared", False)
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
        # Learned lessons: the one part of memory a team benefits from sharing.
        "semantic_memory": TablePolicy(
            LWW, ts_col="updated_at", tombstone_col="is_deleted", shared=True),
        # Episodic memories are written once; last_accessed_at is read state,
        # not modification state, so it must not drive LWW.
        # Never shared: `text` is raw conversation content.
        "episodic_memories": TablePolicy(
            LWW, ts_col="created_at", tombstone_col="is_deleted"),
        # AUTOINCREMENT id is machine-relative: id 7 on two machines refers to
        # two different events. Merge on the natural key, renumber on pack.
        # Never shared: old_value/new_value quote the memories they changed.
        "memory_events": TablePolicy(
            UNION,
            identity=("created_at", "event_type", "memory_type", "memory_key", "source"),
            renumber="id",
            note="append-only event log; ids are machine-local"),
        # Carries embedding_space_sig, which the compatibility gate compares.
        "memory_meta": TablePolicy(LWW, ts_col="updated_at", shared=True),
        "schema_version": TablePolicy(UNION, shared=True),
        "sqlite_sequence": TablePolicy(SKIP, note="managed by SQLite"),
    },
    "knowledge": {
        # The shared library. `items` are referenced by every other table here,
        # so the set travels together or not at all.
        "sources": TablePolicy(LWW, ts_col="updated_at", shared=True),
        "items": TablePolicy(LWW, ts_col="updated_at", shared=True),
        "entities": TablePolicy(LWW, ts_col="updated_at", shared=True),
        "entity_relations": TablePolicy(UNION, shared=True),
        "mentions": TablePolicy(UNION, shared=True),
        "source_locations": TablePolicy(UNION, shared=True),
        "artifact_item_state": TablePolicy(LWW, ts_col="updated_at", shared=True),
        # Tombstones for auto-discovered project directories on *this* machine.
        # Useless to a colleague and it discloses their local layout.
        "dismissed_auto_sources": TablePolicy(UNION),
        # Per-file ingest state. It holds absolute paths, but those are made
        # portable at the sync boundary (ADR 0001), and item_ids links each
        # scanned file to the items built from it. Dropping it would make the
        # receiving machine re-ingest and re-embed a folder it already has --
        # which is exactly what should happen for a colleague, who does not
        # have that folder. Personal by design, despite the cost.
        "folder_file_state": TablePolicy(LWW, ts_col="last_seen"),
        "ingestion_jobs": TablePolicy(LOCAL, note="transient job state"),
    },
}

# Columns that, when present, are good LWW discriminators, in preference order.
_TS_CANDIDATES = ("updated_at", "modified_at", "created_at")


def infer(columns, pk_columns, is_virtual, is_shadow):
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


def for_table(db_name, table_info):
    """Resolve the merge policy for one dbio.TableInfo."""
    override = OVERRIDES.get(db_name, {}).get(table_info.name)
    if override is not None:
        return override
    return infer(table_info.columns, table_info.pk_columns,
                 table_info.is_virtual, table_info.is_shadow)


def is_exported(policy, scope=PERSONAL):
    """True when this table is written to the sync repo under *scope*.

    Team scope is an allowlist: a table has to be explicitly marked shared. An
    unrecognised table -- one a later KiroCrew version adds -- therefore stays
    on the machine rather than reaching colleagues by default.
    """
    if policy.mode in (SKIP, LOCAL):
        return False
    if scope == TEAM:
        return policy.shared
    return True
