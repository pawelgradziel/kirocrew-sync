# TODO

Ideas not yet built. Each entry records what was already checked, so picking
one up does not start from zero.

## Import knowledge from Claude Code / Cursor sessions

Ingest coding-assistant transcripts and project folders into the KiroCrew
knowledge library, so work done in another tool becomes searchable alongside
everything else.

**What is already there**

- KiroCrew ships exactly one knowledge connector:
  `src/kiro_crew/knowledge/connectors/local_folder.py` (plus `base.py`).
  Folder sources are the existing, supported path — pointing a folder source at
  a docs directory already works today and needs nothing new.
- The gap is *conversation* sources, which have no connector at all.

**Where the data lives**

- **Claude Code** — `~/.claude/projects/<project-slug>/*.jsonl`, one JSONL file
  per session, append-only. On this machine: 16 projects, 119 transcripts.
  Structured and easy to parse; the slug encodes the project path.
- **Cursor** — `~/.config/Cursor` (and `~/.cursor`). Chats live in a SQLite
  `state.vscdb` under the workspace storage directories, as JSON blobs in a
  key/value table. Undocumented and version-sensitive, so expect it to break
  across Cursor releases. Harder than Claude Code; do that one first.

**Design note before starting**

Ingestion belongs in KiroCrew, not in kirocrew-sync. This repo moves data
between machines; it does not create it, and a connector living here would have
to duplicate the chunker, embedder and dedup logic that already exist in
`src/kiro_crew/knowledge/`. Two honest options:

1. A connector inside KiroCrew (`connectors/claude_code.py`), matching
   `local_folder.py`. Correct home, needs a change to the app.
2. A standalone importer that writes `sources` + `items` into `knowledge.db`
   directly. Faster to ship, but has to reproduce embedding and chunking to
   stay consistent with `embedding_space_sig`, or the rows it writes will fail
   the embedding gate on the next sync.

Option 1 unless there is a reason the app cannot change.

**Interaction with sync**

Whatever creates the rows, they land in `knowledge.db` and sync like any other
knowledge rows. Note that imported sources carry a `uri`, so ADR 0001 path
portability applies, and in `team` scope those URIs are visible to colleagues.
