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


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create")
    p.add_argument("dir")
    p.add_argument("--name", default="a")
    p.add_argument("--embedding-sig", default="sig-shared")
    p.set_defaults(func=cmd_create)

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
