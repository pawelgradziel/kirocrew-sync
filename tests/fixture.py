#!/usr/bin/env python3
"""Build and mutate throwaway KiroCrew directories for the sync tests.

The schemas mirror the real ones closely enough to exercise the actual merge
policies: text primary keys, updated_at columns, soft deletes, an AUTOINCREMENT
event log, foreign keys, an FTS5 index over external content, and a BLOB
embedding column.
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

MEMORY_SCHEMA = """
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE semantic_memory (
    key TEXT PRIMARY KEY, value_json TEXT NOT NULL, confidence REAL DEFAULT 0.5,
    source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    is_deleted INTEGER DEFAULT 0, embedding BLOB);
CREATE TABLE episodic_memories (
    id TEXT PRIMARY KEY, conversation_id TEXT, text TEXT NOT NULL,
    embedding BLOB, tags TEXT DEFAULT '[]', importance REAL DEFAULT 0.5,
    created_at TEXT NOT NULL, last_accessed_at TEXT, is_deleted INTEGER DEFAULT 0);
CREATE TABLE memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
    memory_type TEXT NOT NULL, memory_key TEXT NOT NULL, old_value TEXT,
    new_value TEXT, source TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE memory_meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
"""

KNOWLEDGE_SCHEMA = """
CREATE TABLE sources (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, source_type TEXT NOT NULL,
    uri TEXT UNIQUE NOT NULL, properties TEXT DEFAULT '{}', last_synced TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE items (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
    item_type TEXT NOT NULL, source_id TEXT REFERENCES sources(id),
    chunk_index INTEGER DEFAULT 0, namespace TEXT DEFAULT 'default',
    summary TEXT, tags TEXT DEFAULT '[]', embedding BLOB,
    status TEXT DEFAULT 'active', content_hash TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    embedding_sig TEXT, embedded_at TEXT);
CREATE TABLE entities (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, entity_type TEXT NOT NULL,
    description TEXT, aliases TEXT DEFAULT '[]',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE mentions (
    item_id TEXT NOT NULL REFERENCES items(id),
    entity_id TEXT NOT NULL REFERENCES entities(id),
    context TEXT, created_at TEXT NOT NULL, PRIMARY KEY (item_id, entity_id));
CREATE TABLE ingestion_jobs (
    id TEXT PRIMARY KEY, source_id TEXT REFERENCES sources(id),
    status TEXT DEFAULT 'pending', created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE TABLE folder_file_state (
    source_id TEXT NOT NULL REFERENCES sources(id), file_path TEXT NOT NULL,
    content_hash TEXT, mtime REAL, last_seen TEXT NOT NULL,
    PRIMARY KEY (source_id, file_path));
CREATE VIRTUAL TABLE items_fts USING fts5(
    title, content, tags, content=items, content_rowid=rowid);
"""

T0 = "2026-01-01T00:00:00+00:00"


def connect(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def cmd_create(args):
    root = Path(args.dir)
    (root / "workspace" / "knowledge").mkdir(parents=True, exist_ok=True)
    (root / "sessions").mkdir(parents=True, exist_ok=True)

    mem = connect(root / "memory.db")
    mem.executescript(MEMORY_SCHEMA)
    mem.execute("INSERT INTO schema_version VALUES (3, ?)", [T0])
    mem.execute("INSERT INTO memory_meta VALUES ('embedding_space_sig', ?, ?)",
                [args.embedding_sig, T0])
    # Shared starting state, so later syncs have a real merge base.
    for key, rule in [("lesson.common", "shared rule"),
                      ("lesson.conflict", "original"),
                      ("lesson.doomed", "to be deleted")]:
        mem.execute(
            "INSERT INTO semantic_memory (key, value_json, source, created_at,"
            " updated_at, embedding) VALUES (?,?,?,?,?,?)",
            [key, json.dumps({"rule": rule}), "seed", T0, T0,
             bytes(range(64)) * 16])
    mem.execute(
        "INSERT INTO memory_events (event_type, memory_type, memory_key,"
        " source, created_at) VALUES ('create','semantic','lesson.common',"
        " 'seed', ?)", [T0])
    # Raw conversation text. Travels between one person's own machines; must
    # never reach a colleague in team scope.
    mem.execute(
        "INSERT INTO episodic_memories (conversation_id, text, created_at)"
        " VALUES (?,?,?)",
        ["conv-" + args.name, "private conversation on " + args.name, T0])
    mem.commit()
    mem.execute("PRAGMA journal_mode=WAL")
    mem.close()

    kn = connect(root / "workspace" / "knowledge" / "knowledge.db")
    kn.executescript(KNOWLEDGE_SCHEMA)
    kn.execute("INSERT INTO sources VALUES ('src-1','Artifacts','artifact',"
               "'artifact://','{}',NULL,?,?)", [T0, T0])
    kn.execute(
        "INSERT INTO items (id,title,content,item_type,source_id,content_hash,"
        "created_at,updated_at,embedding,embedding_sig) "
        "VALUES ('item-common','Shared doc','shared body','design_doc','src-1',"
        "'h1',?,?,?,?)", [T0, T0, bytes(range(256)) * 16, args.embedding_sig])
    kn.execute("INSERT INTO entities VALUES ('ent-1','KiroCrew','product',"
               "'the app','[]',?,?)", [T0, T0])
    kn.execute("INSERT INTO mentions VALUES ('item-common','ent-1','ctx',?)", [T0])
    # Machine-local tables: must never appear on the other side.
    kn.execute("INSERT INTO ingestion_jobs VALUES (?,'src-1','done',?,?)",
               ["job-" + args.name, T0, T0])
    kn.execute("INSERT INTO folder_file_state VALUES ('src-1',?,'h',1.0,?)",
               ["/machine/%s/local/path" % args.name, T0])
    kn.commit()
    kn.execute("PRAGMA journal_mode=WAL")
    kn.close()

    (root / "config.json").write_text(json.dumps({
        "default_agent": "shared",
        "telegram": {"bot_token": "SECRET-%s" % args.name, "enabled": True},
        "session": {"limit": 10},
    }, indent=2), encoding="utf-8")
    (root / "tags.json").write_text(json.dumps({"tags": ["a", "b"]}), encoding="utf-8")
    (root / "sessions" / ("chat-%s.jsonl" % args.name)).write_text(
        json.dumps({"role": "user", "text": "hello from " + args.name}) + "\n",
        encoding="utf-8")
    # The older turns of that same conversation, rolled out of the live
    # transcript the way KiroCrew's history.py does it. Same conversation,
    # second file -- so it has to travel with the first one or the session
    # reads as truncated on the other machine.
    archive = root / "sessions" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / ("chat-%s__20260101-000000.jsonl" % args.name)).write_text(
        json.dumps({"role": "user", "text": "older turn from " + args.name}) + "\n",
        encoding="utf-8")
    # Must never leave the machine.
    (root / "mcp.json").write_text(
        json.dumps({"mcpServers": {"x": {"env": {"API_KEY": "live-token"}}}}),
        encoding="utf-8")
    (root / ".local_secret").write_text("topsecret", encoding="utf-8")
    # A checked-out repo nested inside an allowlisted tree. artifacts/** is
    # recursive, so only the denylist keeps .git out -- and .git/config
    # routinely carries a credential in the remote URL.
    git_dir = root / "artifacts" / "cloned-repo" / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "config").write_text(
        "[remote \"origin\"]\n\turl = https://x-token:ghp-gitsecret@example.com/r.git\n",
        encoding="utf-8")
    (root / "artifacts" / "cloned-repo" / ("notes-%s.md" % args.name)).write_text(
        "ordinary artifact content from %s\n" % args.name, encoding="utf-8")
    print("created %s" % root)
    return 0


def cmd_create_empty(args):
    """A freshly-installed KiroCrew: real schema, zero content rows.

    Distinct from `create`, which seeds baseline shared rows so later syncs
    have a real merge base for merge testing. This simulates what `import`'s
    existing-state gate is actually deciding between: KiroCrew has been
    installed and run once, so its databases exist with the current schema,
    but nothing has been added to it yet -- there is nothing here an import
    would discard.
    """
    root = Path(args.dir)
    (root / "workspace" / "knowledge").mkdir(parents=True, exist_ok=True)
    (root / "sessions").mkdir(parents=True, exist_ok=True)

    mem = connect(root / "memory.db")
    mem.executescript(MEMORY_SCHEMA)
    mem.execute("INSERT INTO schema_version VALUES (3, ?)", [T0])
    if args.embedding_sig:
        mem.execute("INSERT INTO memory_meta VALUES ('embedding_space_sig', ?, ?)",
                    [args.embedding_sig, T0])
    mem.commit()
    mem.execute("PRAGMA journal_mode=WAL")
    mem.close()

    kn = connect(root / "workspace" / "knowledge" / "knowledge.db")
    kn.executescript(KNOWLEDGE_SCHEMA)
    kn.commit()
    kn.execute("PRAGMA journal_mode=WAL")
    kn.close()

    print("created empty %s" % root)
    return 0


def cmd_set_lesson(args):
    conn = connect(Path(args.dir) / "memory.db")
    conn.execute(
        "INSERT INTO semantic_memory (key,value_json,source,created_at,"
        "updated_at,is_deleted) VALUES (?,?,?,?,?,0) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,"
        " updated_at=excluded.updated_at, is_deleted=0",
        [args.key, json.dumps({"rule": args.rule}), "test", args.ts, args.ts])
    conn.execute(
        "INSERT INTO memory_events (event_type,memory_type,memory_key,source,"
        "created_at) VALUES ('update','semantic',?,?,?)",
        [args.key, "machine-" + args.dir[-1], args.ts])
    conn.commit()
    conn.close()
    return 0


def cmd_del_lesson(args):
    conn = connect(Path(args.dir) / "memory.db")
    conn.execute("DELETE FROM semantic_memory WHERE key=?", [args.key])
    conn.commit()
    conn.close()
    return 0


def cmd_add_item(args):
    conn = connect(Path(args.dir) / "workspace" / "knowledge" / "knowledge.db")
    conn.execute(
        "INSERT OR REPLACE INTO items (id,title,content,item_type,source_id,"
        "content_hash,created_at,updated_at,embedding,embedding_sig) "
        "VALUES (?,?,?,'design_doc','src-1',?,?,?,?,'sig-shared')",
        [args.id, args.title, "body of " + args.id, "hash-" + args.id,
         args.ts, args.ts, bytes([len(args.id) % 256]) * 4096])
    conn.commit()
    conn.close()
    return 0


def cmd_episodic(args):
    """Raw conversation text held in memory.db, one line per row."""
    conn = connect(Path(args.dir) / "memory.db")
    for (text,) in conn.execute("SELECT text FROM episodic_memories ORDER BY id"):
        print(text)
    conn.close()
    return 0


def cmd_set_embedding_sig(args):
    """Move a machine onto a different embedding model, as an upgrade would."""
    conn = connect(Path(args.dir) / "memory.db")
    conn.execute("UPDATE memory_meta SET value=? WHERE key='embedding_space_sig'",
                 [args.sig])
    conn.commit()
    conn.close()
    return 0


def cmd_set_config(args):
    path = Path(args.dir) / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    node = data
    parts = args.key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = args.value
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return 0


def cmd_dump(args):
    """Canonical dump of everything that is supposed to converge."""
    root = Path(args.dir)
    out = {}

    conn = connect(root / "memory.db")
    out["lessons"] = [dict(r) for r in conn.execute(
        "SELECT key, value_json, is_deleted FROM semantic_memory ORDER BY key")]
    out["events"] = [dict(r) for r in conn.execute(
        "SELECT event_type, memory_key, source, created_at FROM memory_events "
        "ORDER BY created_at, memory_key, source")]
    out["event_ids"] = [r[0] for r in conn.execute(
        "SELECT id FROM memory_events ORDER BY id")]
    conn.close()

    conn = connect(root / "workspace" / "knowledge" / "knowledge.db")
    out["items"] = [dict(r) for r in conn.execute(
        "SELECT id, title, updated_at, length(embedding) AS emb "
        "FROM items ORDER BY id")]
    out["entities"] = [dict(r) for r in conn.execute(
        "SELECT id, name FROM entities ORDER BY id")]
    out["fts_hits"] = [r[0] for r in conn.execute(
        "SELECT id FROM items WHERE rowid IN "
        "(SELECT rowid FROM items_fts WHERE items_fts MATCH 'body') ORDER BY id")]
    conn.close()

    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    # Credentials are deliberately machine-local, so they must not count
    # towards convergence. cmd_local_only asserts on them separately.
    config.get("telegram", {}).pop("bot_token", None)
    out["config"] = config
    out["sessions"] = sorted(p.name for p in (root / "sessions").glob("*.jsonl"))
    out["session_archives"] = sorted(
        p.name for p in (root / "sessions" / "archive").glob("*.jsonl"))
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def cmd_local_only(args):
    """Report values that must stay machine-local."""
    root = Path(args.dir)
    conn = connect(root / "workspace" / "knowledge" / "knowledge.db")
    out = {
        "jobs": [r[0] for r in conn.execute(
            "SELECT id FROM ingestion_jobs ORDER BY id")],
        "folder_paths": [r[0] for r in conn.execute(
            "SELECT file_path FROM folder_file_state ORDER BY file_path")],
    }
    conn.close()
    secret = json.loads((root / "config.json").read_text(encoding="utf-8"))
    out["bot_token"] = secret.get("telegram", {}).get("bot_token")
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


# --------------------------------------------------------------------------
# Member memory stores (<data home>/memory_stores/<name>/)
# --------------------------------------------------------------------------

# Upstream KiroCrew 0.7's member database, as vector_memory.create_member_database
# builds it: memory_schema.CREW_SCHEMA_SQL (trimmed to the indexes that matter
# here) + MEMBER_SCHEMA_SQL + memory_record_metadata.ensure_schema + the
# schema_version table. Views and the FTS5 table included: a store created on
# the other machine has to come out with all of them.
STORE_SCHEMA = """
CREATE TABLE memory_items (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('directive', 'fact', 'episode')),
    key TEXT, text TEXT NOT NULL, value_json TEXT, embedding BLOB,
    conversation_id TEXT, tags TEXT NOT NULL DEFAULT '[]',
    scope TEXT NOT NULL DEFAULT '', surface TEXT NOT NULL DEFAULT '',
    crew TEXT NOT NULL DEFAULT '', session_key TEXT NOT NULL DEFAULT '',
    derived_from TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0.5, confidence REAL NOT NULL DEFAULT 0.5,
    source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    last_accessed_at TEXT, is_deleted INTEGER NOT NULL DEFAULT 0,
    UNIQUE (key));
CREATE INDEX idx_mi_kind_live ON memory_items (kind, is_deleted);
CREATE VIEW semantic_memory AS
    SELECT key, value_json, confidence, source, created_at, updated_at, is_deleted,
           embedding
      FROM memory_items WHERE kind IN ('directive', 'fact');
CREATE VIEW episodic_memories AS
    SELECT id, conversation_id, text, embedding, tags, importance, created_at,
           last_accessed_at, is_deleted
      FROM memory_items WHERE kind = 'episode';
CREATE TABLE memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
    memory_type TEXT NOT NULL, memory_key TEXT NOT NULL, old_value TEXT,
    new_value TEXT, source TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE memory_meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE member_database (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    format_version INTEGER NOT NULL, member_id TEXT NOT NULL,
    store_id TEXT NOT NULL);
CREATE TABLE memory_history (
    day TEXT PRIMARY KEY, content TEXT NOT NULL, revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL);
CREATE TABLE memory_consolidations (
    source_id TEXT PRIMARY KEY, source_total INTEGER NOT NULL,
    source_count INTEGER NOT NULL, source_digest TEXT NOT NULL,
    receipt_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE VIRTUAL TABLE memory_fts USING fts5(path UNINDEXED, content);
CREATE TABLE memory_record_meta (
    record_id TEXT PRIMARY KEY, kind TEXT NOT NULL, revision INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE TABLE memory_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL,
    revision INTEGER NOT NULL, base_revision INTEGER NOT NULL,
    status TEXT NOT NULL, operation TEXT NOT NULL, source TEXT NOT NULL,
    before_json TEXT, after_json TEXT, metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT);
"""


def _store_db(root, name):
    return Path(root) / "memory_stores" / name / "memory.db"


def _store_write(conn, key, text, ts, source):
    """One semantic write the way the engine records it: row, event, revision."""
    row = conn.execute("SELECT revision FROM memory_record_meta WHERE record_id=?",
                       ["key:" + key]).fetchone()
    revision = (row[0] if row else 0) + 1
    kind = "directive" if key.startswith("lesson.") else "fact"
    conn.execute(
        "INSERT INTO memory_items (id,kind,key,text,value_json,source,created_at,"
        "updated_at,embedding) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET text=excluded.text,"
        " value_json=excluded.value_json, updated_at=excluded.updated_at,"
        " is_deleted=0",
        ["key:" + key, kind, key, text, json.dumps(text), source, ts, ts,
         bytes([len(key) % 256]) * 1024])
    conn.execute(
        "INSERT INTO memory_record_meta VALUES (?,?,?,'active',?,?) "
        "ON CONFLICT(record_id) DO UPDATE SET revision=excluded.revision,"
        " content_hash=excluded.content_hash, updated_at=excluded.updated_at",
        ["key:" + key, kind, revision, "hash-" + text, ts])
    conn.execute(
        "INSERT INTO memory_revisions (record_id,revision,base_revision,status,"
        "operation,source,after_json,metadata_json,created_at) "
        "VALUES (?,?,?,'accepted','upsert',?,?,'{}',?)",
        ["key:" + key, revision, revision - 1, source, json.dumps(text), ts])
    conn.execute(
        "INSERT INTO memory_events (event_type,memory_type,memory_key,source,"
        "created_at) VALUES ('update','semantic',?,?,?)", [key, source, ts])
    # KiroCrew maintains memory_fts on write; the sync never exports it.
    conn.execute("INSERT INTO memory_fts(path,content) VALUES (?,?)",
                 ["key:" + key, key + " " + text])


def cmd_create_store(args):
    """A member store as upstream provisions it, plus host-local neighbours."""
    root = Path(args.dir)
    stores_root = root / "memory_stores"
    directory = stores_root / args.name
    (directory / "memory").mkdir(parents=True, exist_ok=True)
    conn = connect(directory / "memory.db")
    conn.executescript(STORE_SCHEMA)
    conn.execute("INSERT INTO schema_version VALUES (1001, ?)", [T0])
    conn.execute("INSERT INTO member_database VALUES (1, 1, ?, ?)",
                 [args.member_id, args.name])
    conn.execute("INSERT INTO memory_meta VALUES ('schema_lineage','crew',?)", [T0])
    conn.execute("INSERT INTO memory_meta VALUES ('embedding_space_sig',?,?)",
                 [args.embedding_sig, T0])
    _store_write(conn, "lesson.seed-" + args.name, "seed lesson of " + args.name,
                 T0, "seed")
    conn.commit()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    (directory / "memory" / "preferences.md").write_text(
        "# Member Preferences\n- prefers tabs (%s)\n" % args.name, encoding="utf-8")
    (directory / "memory" / "projects.md").write_text(
        "# Member Projects\n", encoding="utf-8")
    # Host-local, per upstream memory_stores.is_host_local_store_state. None
    # of these may ever be published.
    (stores_root / ".member-api-key").write_text(
        "MEMBER-API-KEY-%s" % args.name, encoding="utf-8")
    backups = stores_root / ".member-backups" / args.name
    backups.mkdir(parents=True, exist_ok=True)
    (backups / "memory.20260101T000000Z.md").write_text(
        "MEMBER-BACKUP-CONTENT-%s" % args.name, encoding="utf-8")
    (backups / ".store-use.lock").write_text("", encoding="utf-8")
    logs = stores_root / ".execution-logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "run.log").write_text("EXECUTION-LOG-%s" % args.name, encoding="utf-8")
    (directory / "backups").mkdir(exist_ok=True)
    (directory / "backups" / "old.md").write_text(
        "STORE-BACKUP-CONTENT-%s" % args.name, encoding="utf-8")
    print("created store %s" % directory)
    return 0


def cmd_store_write(args):
    conn = connect(_store_db(args.dir, args.name))
    _store_write(conn, args.key, args.text, args.ts, "machine-" + args.dir[-1])
    conn.commit()
    conn.close()
    return 0


def cmd_store_episode(args):
    """Raw conversation text, kind 'episode' in the same table as lessons."""
    conn = connect(_store_db(args.dir, args.name))
    conn.execute(
        "INSERT INTO memory_items (id,kind,text,conversation_id,source,"
        "created_at,updated_at) VALUES (?,'episode',?,?,'consolidation',?,?)",
        [args.id, args.text, "conv-" + args.id, args.ts, args.ts])
    conn.execute("INSERT INTO memory_fts(path,content) VALUES (?,?)",
                 [args.id, " " + args.text])
    conn.commit()
    conn.close()
    return 0


def cmd_store_history(args):
    conn = connect(_store_db(args.dir, args.name))
    conn.execute(
        "INSERT INTO memory_history VALUES (?,?,1,?) ON CONFLICT(day) DO UPDATE"
        " SET content=excluded.content, revision=revision+1,"
        " updated_at=excluded.updated_at", [args.day, args.content, args.ts])
    conn.commit()
    conn.close()
    return 0


def cmd_store_sql(args):
    """Run one statement against a store (schema drift, signature changes)."""
    conn = connect(_store_db(args.dir, args.name))
    conn.execute(args.sql)
    conn.commit()
    conn.close()
    return 0


def cmd_store_dump(args):
    """Everything in every store that is supposed to converge."""
    root = Path(args.dir) / "memory_stores"
    out = {}
    if root.is_dir():
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            db = directory / "memory.db"
            if not db.exists():
                continue
            conn = connect(db)
            entry = {
                "identity": [list(r) for r in conn.execute(
                    "SELECT member_id, store_id FROM member_database")],
                "items": [dict(r) for r in conn.execute(
                    "SELECT id, kind, key, text, is_deleted, updated_at,"
                    " length(embedding) AS emb FROM memory_items ORDER BY id")],
                "events": [dict(r) for r in conn.execute(
                    "SELECT id, memory_key, source, created_at FROM memory_events"
                    " ORDER BY id")],
                "revisions": [dict(r) for r in conn.execute(
                    "SELECT id, record_id, revision, created_at FROM"
                    " memory_revisions ORDER BY id")],
                "history": [dict(r) for r in conn.execute(
                    "SELECT day, content FROM memory_history ORDER BY day")],
                "fts": sorted(r[0] for r in conn.execute(
                    "SELECT path FROM memory_fts")),
                "views": sorted(r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='view'")),
                "lessons_view": [r[0] for r in conn.execute(
                    "SELECT key FROM semantic_memory ORDER BY key")],
            }
            conn.close()
            prefs = directory / "memory" / "preferences.md"
            entry["preferences"] = (prefs.read_text(encoding="utf-8")
                                    if prefs.exists() else None)
            out[directory.name] = entry
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-store")
    p.add_argument("dir"); p.add_argument("name")
    p.add_argument("--member-id", default="alice")
    p.add_argument("--embedding-sig", default="sig-shared")
    p.set_defaults(func=cmd_create_store)

    p = sub.add_parser("store-write")
    p.add_argument("dir"); p.add_argument("name")
    p.add_argument("key"); p.add_argument("text")
    p.add_argument("--ts", default="2026-02-01T00:00:00+00:00")
    p.set_defaults(func=cmd_store_write)

    p = sub.add_parser("store-episode")
    p.add_argument("dir"); p.add_argument("name")
    p.add_argument("id"); p.add_argument("text")
    p.add_argument("--ts", default="2026-02-01T00:00:00+00:00")
    p.set_defaults(func=cmd_store_episode)

    p = sub.add_parser("store-history")
    p.add_argument("dir"); p.add_argument("name")
    p.add_argument("day"); p.add_argument("content")
    p.add_argument("--ts", default="2026-02-01T00:00:00+00:00")
    p.set_defaults(func=cmd_store_history)

    p = sub.add_parser("store-sql")
    p.add_argument("dir"); p.add_argument("name"); p.add_argument("sql")
    p.set_defaults(func=cmd_store_sql)

    p = sub.add_parser("store-dump")
    p.add_argument("dir")
    p.set_defaults(func=cmd_store_dump)

    p = sub.add_parser("create")
    p.add_argument("dir")
    p.add_argument("--name", default="a")
    p.add_argument("--embedding-sig", default="sig-shared")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("create-empty")
    p.add_argument("dir")
    p.add_argument("--name", default="a")
    p.add_argument("--embedding-sig", default="")
    p.set_defaults(func=cmd_create_empty)

    p = sub.add_parser("set-lesson")
    p.add_argument("dir"); p.add_argument("key"); p.add_argument("rule")
    p.add_argument("--ts", default="2026-02-01T00:00:00+00:00")
    p.set_defaults(func=cmd_set_lesson)

    p = sub.add_parser("del-lesson")
    p.add_argument("dir"); p.add_argument("key")
    p.set_defaults(func=cmd_del_lesson)

    p = sub.add_parser("add-item")
    p.add_argument("dir"); p.add_argument("id"); p.add_argument("title")
    p.add_argument("--ts", default="2026-02-01T00:00:00+00:00")
    p.set_defaults(func=cmd_add_item)

    p = sub.add_parser("episodic")
    p.add_argument("dir")
    p.set_defaults(func=cmd_episodic)

    p = sub.add_parser("set-embedding-sig")
    p.add_argument("dir"); p.add_argument("sig")
    p.set_defaults(func=cmd_set_embedding_sig)

    p = sub.add_parser("set-config")
    p.add_argument("dir"); p.add_argument("key"); p.add_argument("value")
    p.set_defaults(func=cmd_set_config)

    p = sub.add_parser("dump")
    p.add_argument("dir")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("local-only")
    p.add_argument("dir")
    p.set_defaults(func=cmd_local_only)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
