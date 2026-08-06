# 1. Knowledge base path portability

- **Status:** Accepted
- **Date:** 2026-08-06
- **Deciders:** kirocrew-sync maintainers
- **Supersedes:** the "Implement Option 1 (Tilde Normalization)" recommendation in
  *KiroCrew Knowledge Base: Path Portability Problem v1*

## Context

KiroCrew stores a knowledge folder source as a plain filesystem path in
`sources.uri`, exactly as the user typed it. Syncing `knowledge.db` to a second
machine carries that path along unchanged, so a source added as
`/home/alice/dev/groover/docs` is dead on any machine without that directory:
the source fails to load, the items behind it go unreachable, and the user has
to re-add sources after every sync.

Three variants break in different ways:

| Stored as | Cross-machine | Why |
|---|---|---|
| `/home/alice/dev/repo` | ✗ | Hardcodes username and layout |
| `~/dev/repo` | ~ | Works only if both machines share the layout |
| `./docs` | ✗ | Resolves against whatever CWD the reader has |

The problem statement proposed four options and recommended normalizing stored
URIs to tilde paths (Option 1), applied in `add_source()` and in this sync tool.

### The finding that changed the decision

**KiroCrew does not expand `~` when resolving source URIs.**

- `src/kiro_crew/knowledge/connectors/local_folder.py` — `p = Path(uri)`
- `src/kiro_crew/knowledge/folder_watcher.py` — `Path(uri).is_dir()`

`Path("~/code/docs").exists()` is always `False`. Writing tilde paths into the
live database would therefore break every folder source on **every** machine,
including the one that created it — turning a cross-machine bug into a
universal one. Any option that changes what the live database stores is
constrained by this, and this tool cannot change the application.

A second constraint surfaced while implementing: `sources.uri` is `UNIQUE`, and
`folder_file_state` is keyed on `(source_id, file_path)`. Normalization can make
two rows collide, so a rewrite must be able to fail on a single row without
losing it.

## Decision drivers

- Must not break the live database that KiroCrew reads.
- Must not require changes to the KiroCrew application to ship.
- Must handle the layouts users actually have, including paths outside `$HOME`.
- Must never silently point a knowledge source at the wrong directory.
- Must degrade safely: an old bundle, a missing `python3`, or an unknown schema
  version cannot break a sync.

## Options considered

### Option 1 — Normalize stored URIs to tilde paths ✗ Rejected

Rewrite `sources.uri` to `~/...` at `add_source()` time and in the database.

Simple and cheap, but it stores a value KiroCrew cannot resolve (see above), and
it does nothing for paths outside `$HOME` or for machines whose `$HOME` layouts
differ. Rejected on correctness, not on cost.

### Option 2 — Path mapping configuration ✗ Rejected on its own

A user-declared `from`/`to` mapping table applied to the database during sync.

Handles the hard cases Option 1 cannot, but as specified it rewrites the live
database with `REPLACE(uri, ...)` — string surgery with no notion of path
boundaries — and needs configuration before it does anything at all. Its core
idea (name a location once per machine) is kept; its mechanism is not.

### Option 3 — Workspace-relative symlinks ✗ Rejected

Store sources as symlinks under the workspace and record relative URIs.

Requires manual symlink repair on every new machine, needs admin rights on
Windows, and changes what the application stores. Cost is high and the manual
step defeats the goal.

### Option 4 — Dynamic path resolution service ✗ Rejected

Store `${HOME}/code/${PROJECT}/docs` patterns, resolve at query time.

Maximum flexibility, but resolution has to live inside the application to work,
which is exactly what this tool cannot change. The variable syntax is worth
keeping; the runtime service is not.

### Option 5 — Translate at the sync boundary ✅ **Chosen**

Keep the live database machine-local. Translate only the copy in transit:

```
push:  /home/alice/dev/groover/docs  →  ~/dev/groover/docs        (bundle copy)
pull:  ~/dev/groover/docs            →  /Users/pawel/dev/groover/docs
```

Portable form is `~/...` for anything under `$HOME`, and `${NAME}/...` for
locations a machine has named in `path_map.conf` — Option 1's default behaviour
with Option 2's escape hatch, applied where neither breaks the application.

Three columns travel: `sources.uri`, `folder_file_state.file_path`, and
`dismissed_auto_sources.uri`. Per-file ingest state moves with its source, so a
synced machine matches the same files by hash instead of re-ingesting them.

## Consequences

**Positive**

- The application needs no change, and never sees a path it cannot resolve.
- The live database is never opened for writing, so a failed translation cannot
  damage local data.
- Paths outside `$HOME` and mismatched `$HOME` layouts are solved, not deferred.
- Both directions are idempotent and no-ops on already-correct values, so
  bundles from older and newer clients interoperate.
- Falls back to today's behaviour with a warning when `python3` is absent.

**Negative**

- Translation is tied to `push`/`pull`. A database copied by other means (manual
  `scp`, a file-sync daemon watching `~/.kiro/crew`) gets no translation.
- Two representations exist — stored and in-transit — which is one more concept
  for a reader of the code. Mitigated by confining it to `lib/portable_paths.py`
  and stating it at the top of that file.
- The path map is a per-machine file the user must write for the harder cases.
- Column coverage is a snapshot of the schema. A future KiroCrew release that
  stores paths somewhere new needs a matching entry in `TARGETS`.

**Neutral / deliberately out of scope**

- **Relative URIs** (`./docs`) are reported and left alone; they resolve against
  the CWD of whatever process added them, which is not knowable later.
- **Symlinks are not resolved.** The logical path (`~/projects/groover`) travels
  better than its target on one machine (`/mnt/storage/repos/groover`).
- **Collisions are never merged.** When normalization makes two rows equal, the
  row is left untouched and reported: merging would take items, source locations
  and ingest state with it.
- Other synced data (`config.json`, `memory.db`) may hold machine-specific
  paths. Not addressed here.

## Implementation

- `lib/portable_paths.py` — `encode` / `decode` / `report`, standard library only.
- `kirocrew-sync.sh` — `encode` after bundling on push, `decode` before applying
  on pull, plus a `paths` command that reports how each source stands.
- `path_map.conf.example` — per-machine location names; never synced.
- `SYNC_PORTABLE_PATHS=0` opts out; `KIROCREW_PATH_MAP` relocates the map.

Two defects were fixed because this decision depends on them:

1. `check_kirocrew_running` matched the sync script's own command line, so
   `push` and `pull` always refused to run.
2. Checkpointing a bundled database leaves it without a `-wal`; a receiver's
   stale `-wal` would be replayed onto it. `apply_sync_bundle` now drops
   orphaned `-wal`/`-shm` files.

## Validation

`tests/test_portable_paths.sh` (40 assertions) and `tests/test_sync_paths.sh`
(10 assertions) simulate a second machine by overriding `$HOME`, covering the
original testing checklist plus collisions, WAL handling, unmapped variables,
schema tolerance and malformed maps.

## Revisit if

- KiroCrew starts expanding `~` when resolving source URIs — storing portable
  paths directly then becomes viable and this layer could be retired.
- Users sync `~/.kiro/crew` with a file-sync daemon rather than this tool, which
  bypasses the translation entirely.
- A KiroCrew release stores filesystem paths in columns beyond the three listed.
