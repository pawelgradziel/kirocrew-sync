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

## Sync the kiro-cli half of a session

A session has two halves on disk and only one of them is inside the sync root.

**What is already checked** (against KiroCrew v0.5.0-insider.3)

- KiroCrew writes crew transcripts to `<data home>/sessions/*.jsonl`, with the
  older turns of a long conversation rolled into
  `sessions/archive/<key>__<stamp>.jsonl`. Both now sync — see
  [docs/upstream-sync-review-2026-08-29.md](docs/upstream-sync-review-2026-08-29.md).
- kiro-cli writes its own replay logs to `kiro_sessions_dir()` =
  `<kiro home>/sessions/cli`, i.e. `~/.kiro/sessions/cli` — **outside**
  `~/.kiro/crew`. `config/paths.py` honors `KIRO_HOME` for it.
- Upstream treats the pair as one unit: the AWS Control app's backup module
  tars "BOTH session halves" together and calls it an invariant.
- `session_storage.py` scans both directories to build one session inventory,
  so a machine with only the crew half has sessions whose replay log is
  missing.

**Update 2026-09-22** — `session_map.json` no longer syncs (it was being
pruned on every machine without the CLI half, and the deletion merged back).
Upstream's session export/import now carries this half as "Layer B"; its
constraints (byte-exact files, machine-specific envelope fields, on-loop map
join) and a bundle-based alternative to a second sync root are in
[docs/upstream-sync-review-2026-09-22.md](docs/upstream-sync-review-2026-09-22.md).

**Decided 2026-09-22** — see
[ADR 0003](docs/adr/0003-kiro-cli-session-half.md) (Proposed). `sync` will not
carry this half: no second sync root, and `session_map.json` stays `DENY`'d.
A resumable copy moves through the separate, explicit
`send-session --include-layer-b` / `inbox --install` commands (see the README),
which carry upstream `.kcsession.json.gz` bundles and install them via
`POST /api/chat/slots/import`, so upstream does the envelope rewrite and the
map join. That is not continuous sync: every send makes a new copy on the
other side. The ADR lists what would reopen the raw-file approach below.

**Why it is not a one-line fix** (kept for context; superseded by ADR 0003)

`ALLOW` is relative to `KIROCREW_DIR`, so there is no glob that reaches
`~/.kiro/sessions/cli`. It needs a second sync root: a `KIRO_HOME`-derived
path, its own allowlist, and a decision about what `pack` does when the two
halves disagree. Team scope needs nothing — CLI replay logs are transcripts
and stay home for the same reason the crew half does.
