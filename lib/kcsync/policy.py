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
# A named V1 store's own memory_stores/<name>/memory_index.db is the same kind
# of file; it is kept out by files.DENY's "**/*.db" and never discovered as a
# store database (stores.py only looks for memory.db).
DERIVED_DATABASES = [
    "memory_index.db",
]

# Per-member memory stores are discovered, not listed: one database per
# directory under <data home>/memory_stores/, named by the operator or by
# KiroCrew (`member-<member id>-<uuid>`). Each is its own logical database,
# `memory_stores/<name>`, so it gets its own _schema.sql, its own drift check
# and its own embedding check. lib/kcsync/stores.py owns discovery and name
# validation; every discovered database shares the one override table below.
STORE_DB_PREFIX = "memory_stores/"
STORE_POLICY_KEY = "memory_stores/*"


def is_store_db(db_name):
    """True for a discovered `memory_stores/<name>` logical database."""
    return isinstance(db_name, str) and db_name.startswith(STORE_DB_PREFIX)


def overrides_for(db_name):
    """The explicit override table that applies to *db_name*."""
    if is_store_db(db_name):
        return OVERRIDES[STORE_POLICY_KEY]
    return OVERRIDES.get(db_name, {})

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
        # Added by KiroCrew 0.5.x. Upstream documents it as the same shape and
        # role as artifact_item_state, for the aggregate `agent://` "Auto-added"
        # source: per-document state so one document can be replaced or removed
        # without touching the rest. Shared for the same reason its twin is --
        # `sources` and `items` already travel, and this is the per-document
        # unit that makes those items independently removable at the other end.
        # Its `slug` is content-derived (agent_source.document_slug), not a
        # filesystem path, so unlike folder_file_state it discloses no local
        # layout.
        "agent_item_state": TablePolicy(LWW, ts_col="updated_at", shared=True),
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
    # One crew member's private memory.db (KiroCrew 0.7 "isolated memory v2":
    # upstream memory_schema.CREW_SCHEMA_SQL + MEMBER_SCHEMA_SQL), and a named
    # V1 store's memory.db, which is the same crew lineage without the member
    # tables.
    #
    # Nothing here is `shared`, and stores.py does not even discover stores in
    # team scope. Upstream builds this memory to be private to one member, and
    # team scope is opt-in. The one part a team might want, lessons, cannot be
    # split out: on this lineage a lesson is a `memory_items` row (kind
    # 'directive', key 'lesson.*') in the same table as raw episodes, and the
    # engine publishes whole tables. memory.db's semantic_memory can be shared
    # because it holds no conversation text. memory_items does.
    STORE_POLICY_KEY: {
        # Facts, directives (lessons) and episodes in one table. A semantic row
        # bumps updated_at on every edit; an episode's updated_at is its
        # created_at and never moves. last_accessed_at is read state, so it
        # must not drive LWW (the same reasoning as episodic_memories).
        "memory_items": TablePolicy(
            LWW, ts_col="updated_at", tombstone_col="is_deleted",
            note="crew-lineage memory rows; episodes carry conversation text"),
        # Same table, same shape, same AUTOINCREMENT problem as memory.db's.
        "memory_events": TablePolicy(
            UNION,
            identity=("created_at", "event_type", "memory_type", "memory_key", "source"),
            renumber="id",
            note="append-only event log; ids are machine-local"),
        # Per-record revision journal (upstream memory_record_metadata).
        # AUTOINCREMENT again: id 12 on two machines is two different
        # revisions. Rows are appended and pruned, never updated, so a union on
        # the natural identity is exact. created_at leads the identity so the
        # renumbered ids keep chronological order, which is what upstream's
        # readers assume (ORDER BY id DESC, keep-the-latest-N pruning).
        "memory_revisions": TablePolicy(
            UNION,
            identity=("created_at", "record_id", "revision", "base_revision",
                      "status", "operation", "source"),
            renumber="id",
            note="append-only revision journal; ids are machine-local"),
        "memory_record_meta": TablePolicy(LWW, ts_col="updated_at"),
        # One row per day. KiroCrew rewrites the whole day on every append, so
        # two machines appending to the same day is an edit/edit conflict, and
        # LWW keeps one machine's version of that day (logged as a conflict).
        "memory_history": TablePolicy(LWW, ts_col="updated_at"),
        # Idempotency receipts for consolidation runs, written once per source.
        "memory_consolidations": TablePolicy(LWW, ts_col="created_at"),
        # The store's immutable identity, (member_id, store_id). Neither value
        # is machine-specific: member_id is also in config.json and store_id is
        # the directory name. KiroCrew refuses a member store without this row,
        # so a store created here from the sync repo needs it.
        "member_database": TablePolicy(UNION, note="immutable store identity"),
        # Carries this store's own embedding_space_sig, which the gate compares.
        "memory_meta": TablePolicy(LWW, ts_col="updated_at"),
        "schema_version": TablePolicy(UNION),
        "sqlite_sequence": TablePolicy(SKIP, note="managed by SQLite"),
        # memory_fts and its shadow tables are inferred SKIP (virtual table).
        # After pack they are rebuilt from memory_items + memory_history, the
        # same derivation as upstream's rebuild_memory_index (stores.py).
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
    override = overrides_for(db_name).get(table_info.name)
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
