# ADR 0002: Three-way sync via an unpacked, git-tracked canonical form

- **Status:** Accepted
- **Date:** 2026-08-06
- **Supersedes:** the push/pull file-copy design in the initial release

## Context

`kirocrew-sync` copied a bundle of raw files to a storage backend and copied it
back. A single `sync` command that merges both directions was requested, with
three candidate designs: timestamp comparison, three-way merge with a stored
base manifest, and an operation log with vector clocks.

Before choosing, we inspected a live `~/.kiro/crew`. Several assumptions behind
those designs did not survive contact with the data.

### What the data actually looks like

```
memory.db          4 KB   +  memory.db-wal      190 KB
knowledge.db       4 KB   +  knowledge.db-wal   2.5 MB
```

Both databases run in `journal_mode=wal` and had never been checkpointed. **The
`.db` files are empty shells; all the content is in the `-wal`.** The previous
implementation copied `memory.db` with `cp`, so a "successful" push transferred
a 4 KB stub. It also copied `memory.db-shm`, a shared-memory index that encodes
one process's mmap state and must never be transported.

Further findings:

- **`sqlite3` CLI is not installed.** Any design built on `sqlite3 .dump` piped
  through `diff3` does not run. Python 3 with the stdlib `sqlite3` module (3.46)
  is present.
- **The declared data sources were wrong.** `DATA_SOURCES` referenced
  `data/artifacts.db` and `data/lessons.db`; there is no `data/` directory.
  Artifacts are files under `artifacts/<slug>/`, lessons are rows in
  `memory.db.semantic_memory` keyed `lesson.*`, and `knowledge.db` lives under
  `workspace/knowledge/`.
- **`memory_index.db` is pure derived FTS5 index.** It was being synced.
- **The schemas are well suited to row-level merge.** Nearly every table has a
  stable text primary key, ISO-8601 `created_at`/`updated_at`, `content_hash`,
  and soft-delete flags.
- **`ingestion_jobs` is transient per-machine job state** and is meaningless on
  another machine.
- **Several columns hold absolute filesystem paths.** `sources.uri`,
  `folder_file_state.file_path` and `dismissed_auto_sources.uri` are
  machine-specific as stored. [ADR 0001](0001-knowledge-path-portability.md)
  already solved this for the file-copy design.
- **`memory_events.id` is `INTEGER PRIMARY KEY AUTOINCREMENT`.** Row 7 on two
  machines refers to two different events.
- **The directory contains live credentials.** `mcp.json` held a working Linear
  API token; `config.json` has `bot_token` / `app_password` fields;
  `.local_secret`, `token_signing.key` and `sel_hmac.key` sit alongside.
  `security_events.jsonl` was 31 MB of local audit log.
- **Embedding compatibility is unguarded.** `memory_meta.embedding_space_sig`
  and a per-row `items.embedding_sig` record which model produced each vector.
  Nothing validates them.

### Why the proposed options do not work as written

**Timestamp-based sync** cannot work here at all. `memory.db`'s mtime does not
change while writes land in the WAL, so a machine with 2.5 MB of unsynced
knowledge reports "no local changes". It is also last-write-wins, which is the
defect being fixed.

**Three-way merge over SQL dumps** has the right shape and the wrong mechanism.
`.dump` row order is not stable across machines, so unrelated rows appear as
diffs. `diff3` merges lines and has no notion of a primary key, so two edits to
one row become two `INSERT`s that collide on `PRIMARY KEY`. Embeddings become
4 KB `X'...'` hex literals where one changed byte yields an unmergeable line.
And `.dump` includes FTS5 shadow tables, whose merged inserts silently corrupt
the index.

**Operation-log / CRDT sync** is correctly deferred: it needs KiroCrew core
changes. Worth noting, though, that `memory_events` is already an append-only
op log, and that row-level LWW over `updated_at` with soft-delete tombstones
*is* a CRDT (an LWW-element-set). Most of the convergence benefit is reachable
without touching KiroCrew.

## Decision

Sync operates on an **unpacked canonical form** of KiroCrew's state, tracked in
a **local git repository**, merged **row by row** through custom git merge
drivers, and **packed back** into the live databases.

### 1. Unpack to one JSONL file per table

Each database is exported through SQLite (never by copying files) to one JSONL
file per table: one row per line, keys sorted, lines sorted by primary key.

Reading through SQLite is what makes WAL content visible, and it is why `-wal`
and `-shm` never need to cross the wire. **This alone fixes the empty-database
bug, independent of merging.**

BLOB columns above 512 bytes are externalized to `blob/<sha256>.bin` and
referenced by hash. Identical content on two machines produces an identical
path with identical bytes, so an embedding can never cause a merge conflict.

### 2. Let git track the base and find it

```
~/.kiro/crew/.sync/repo/
  db/memory/semantic_memory.jsonl
  db/memory/_schema.sql          # normalized DDL, one statement per line
  db/memory/_policy.json         # resolved merge policy per table
  db/knowledge/items.jsonl
  blob/<ab>/<sha256>.bin
  files/config.json
  files/sessions/*.jsonl
```

`sync` = unpack → commit → fetch → merge → pack → publish.

Git supplies content addressing, merge-base discovery, conflict handling,
history, rollback, and delta compression. The alternative was hand-building
`base_manifest.json`, `conflict_log.json`, and `merge_rules.json` — a
re-implementation of git, with worse base tracking. Critically, ancestry is
**content-hash based, not timestamp based**, so clock skew between machines
cannot corrupt the merge base.

### 3. Merge per row, via git merge drivers

`.gitattributes` routes each kind of file:

| Path | Driver | Rationale |
|---|---|---|
| `db/**/*.jsonl` | `kcsync-rows` | row-level three-way merge keyed on the PK |
| `db/*/_schema.sql` | `binary` | any divergence must hard-conflict |
| `db/*/_policy.json` | `ours` | regenerated on every unpack |
| `files/**/*.json` | `kcsync-json` | structural merge, key by key |
| `files/sessions/*.jsonl` | `union` | transcripts are append-only (git built-in) |
| `blob/**` | `binary` | content-addressed; identical path implies identical bytes |

The row driver resolves each primary key independently:

| base | local | remote | result |
|---|---|---|---|
| = | = | ≠ | take remote |
| = | ≠ | = | take local |
| ≠ | ≠ | ≠ | policy decides |
| absent | present | absent | insert |
| present | absent | present | delete |

Per-table policy is inferred from the schema — a table with `updated_at` gets
last-writer-wins, a keyed table without one gets set-union, virtual and shadow
tables are skipped — with explicit overrides where the schema does not reveal
the semantics. New tables added by future KiroCrew versions therefore sync
sensibly without code changes.

### 4. Converge, deterministically

Machine A merging B must produce byte-identical output to B merging A, or the
two machines rewrite each other forever. Every automatic resolution is
therefore symmetric: timestamps decide first, and ties fall back to comparing
canonical JSON, which does not depend on which side is "ours".

A delete on one side against an edit on the other keeps the edit. This is the
no-data-loss choice and it still converges: the deleting side adopts the edit on
its next sync.

### 5. Translate paths per row, not per database copy

[ADR 0001](0001-knowledge-path-portability.md) rewrote knowledge paths on a
*copy* of the database while it was in transit. That boundary no longer exists,
so the same translation now happens per row: unpack encodes to the portable
form, pack decodes back to this machine's absolute paths. The rules and the
`path_map.conf` mechanism are unchanged and still live in
`lib/portable_paths.py`; only the point of application moved.

Applying it per row gains something the copy-based version could not have: the
portable form is what the merge compares. Two machines that added the same
folder under different local paths now resolve to one row instead of two
competing ones. It also matters for `folder_file_state`, whose primary key
contains the path — encoding happens before row identity is computed, so both
machines agree on which row is which.

`folder_file_state` is synced rather than treated as machine-local. Its
`item_ids` column links each scanned file to the items built from it, so
dropping it would make the receiving machine re-ingest and re-embed a folder it
already has. `ingestion_jobs` stays local: it is transient job state with no
cross-machine meaning.

### 6. Gate what merging cannot fix

- **Schema drift** — compared against each remote machine *before* merging.
  Different KiroCrew versions are refused rather than blended.
- **Embedding space** — `embedding_space_sig` compared per remote machine
  before merging. Mixing vectors from two models degrades semantic search
  silently, with no error and no visible corruption. This check must run
  pre-merge, because the merge collapses each row to a single winner and the
  difference is gone afterwards.

Both gates are scoped to the machine that fails them. A rejected machine is
*quarantined*: its ref is skipped, every other machine still merges, packs and
publishes, and `sync` exits 3 to report the partial result. Quarantine is the
whole remedy — nothing was merged, so nothing local is at risk, and the check
re-runs on the next sync, so the machine rejoins by itself once it catches up.

Aborting the sync instead, as this originally did, bought no safety and cost
availability: one machine on an old KiroCrew version stopped every other pair
from syncing. Worse, the advertised escape hatch made it dangerous — `--force`
is not per-machine, so the only documented way to unblock A↔B also merged the
incompatible data and skipped the credential gate. The safe action must not be
the one that requires the blunt override.
- **Referential integrity** — deletes run children-first, inserts parents-first
  in foreign-key topological order, followed by `PRAGMA foreign_key_check`.
- **Credentials** — an explicit allowlist decides what leaves the machine, with
  a denylist that always wins on top. Credential-shaped JSON leaves are stripped
  from the synced copy and re-grafted from the local file on pack, so a token
  never reaches the backend and is never clobbered by another machine's value.

### 7. Transport unchanged: one bundle per machine

`git bundle` produces a single file, which fits the existing
`backend_push <dir>` / `backend_pull <dir>` interface with no backend changes.
Bundles are named per machine:

```
remote/bundles/<machine-id>.bundle
```

Each machine writes only its own file, so there is **no write race at the
storage layer**, and the scheme extends past two machines. Syncing a live `.git`
directory through Google Drive is a known way to corrupt a repository; this
avoids it.

## Consequences

### Gained

- The empty-database bug is fixed; 2.5 MB of WAL-resident knowledge now syncs.
- Non-conflicting changes on both machines are both kept.
- Clock skew cannot affect correctness.
- Conflicts are per row, not per file; two machines editing different lessons
  in the same table do not conflict at all.
- Rollback, history, and `git diff` over KiroCrew state come for free.
- Payload shrank: 2.5 MB of WAL becomes ~112 KB of JSONL, delta-compressed.
- Credentials and 31 MB of local audit log stopped being synced.
- More than two machines work.
- Path portability (ADR 0001) now also deduplicates: the same folder added on
  two machines under different local paths merges into one row.

### Costs and limits

- **Python 3.8+ is now a hard dependency.** Bash cannot do this correctly.
  Given that `sqlite3` CLI is absent and `python3` is present, this is the
  better trade, but it is a real new requirement.
- **The wire format changed.** Remote data written by the previous version is
  not readable by this one. Re-seed from a machine with good local data.
- **~2× local disk** for the unpacked repo plus git history. On observed data
  that is about 1 MB.
- **`pack` requires KiroCrew to be stopped.** Reading is safe; writing is not.
- **Third-machine push race.** Backends mirror with delete, so the full bundle
  set is republished each time. If machine C publishes between A's pull and A's
  push, C's bundle is removed from the remote. C's data is intact locally and is
  restored on its next sync — recoverable, not data loss.
- **`config.json` conflicts resolve deterministically, not intelligently.**
  With no timestamp available, ties are broken by canonical JSON comparison.
  It converges and it is logged, but the winner is arbitrary.
- **Schema drift quarantines a machine** rather than migrating. The rest keep
  syncing, but that machine contributes nothing until it is upgraded. Migrating
  instead would require the schema's owner; see *Alternatives considered*.
- **No seeding primitive.** `init` scaffolds config; it cannot pull a first copy
  from a machine that already has good data. Both recovery paths named above —
  re-seeding after the wire-format change, and after the third-machine push race
  — currently depend on one existing. Proposed follow-up under *Alternatives
  considered*.
- **`folder_file_state.mtime` is machine-specific** and travels anyway. It is
  advisory scan state, so a stale value costs at most one re-scan.

## Alternatives considered

| Option | Verdict |
|---|---|
| Timestamp-based (Option 1) | Rejected. mtime does not change under WAL writes, and last-write-wins is the defect being fixed. |
| Base manifest + `sqlite3 .dump` + `diff3` (Option 2) | Right idea, wrong mechanism. `diff3` has no primary-key awareness, mangles BLOBs, corrupts FTS shadow tables, and `sqlite3` is not installed. Adopted its three-way premise, replaced its machinery. |
| Operation log / CRDT (Option 3) | Deferred. Needs KiroCrew core changes. Row-level LWW plus tombstones already provides the convergence property without them. |
| Store base as a second copy of each `.db` | Rejected. Doubles storage of the largest artifacts and still needs a row-level differ. |
| Sync a bare git repo directory through the backend | Rejected. Concurrent writers corrupt git repositories over cloud-sync folders. |
| Snapshot tarball + `restore --mode merge`, as a KiroCrew subcommand | Rejected as a sync mechanism, right about two things. A snapshot carries state without ancestry, so "merge" has no definition. Detailed below. |

### Snapshot + restore as a KiroCrew subcommand

A later proposal moved sync inside the application itself:

```bash
laptop$  kirocrew snapshot --components memory,artifacts
laptop$  scp ~/.kiro/crew/snapshots/latest.tar.gz desktop:~/
desktop$ kirocrew restore ~/latest.tar.gz --mode merge
```

**`--mode merge` cannot be defined.** A snapshot is a state; a merge needs two
states *and their common ancestor*. Restore sees local `L` and snapshot `R`, and
cannot tell these apart:

| Observation | Could mean | Correct action |
|---|---|---|
| in `L`, absent from `R` | this machine added it | keep |
| in `L`, absent from `R` | the other machine deleted it | delete |
| in both, differing | one side edited | take that side |
| in both, differing | both sides edited | conflict |

Nothing in the tarball resolves the ambiguity, so `merge` collapses to either
union — where deletes never propagate and tombstones only accumulate — or
last-writer-wins on `updated_at`, which hands back the clock-skew independence
that §2 and §4 exist to buy. Persisting the previous snapshot to diff against
would resolve it, and would rebuild the `base_manifest.json` that §2 rejects.

Being file-granular costs three further things:

- **Credentials.** `--components` includes or excludes whole files. The secrets
  found here are *inside* files worth syncing — `config.json` holds `bot_token`
  beside real settings — so component scoping cannot reach them. §6 strips per
  JSON leaf instead.
- **Path portability.** A tarball carries `sources.uri` as this machine's
  absolute paths. §5's deduplication depends on encoding *before* row identity is
  computed, which no file-level restore can do.
- **The empty-database bug returns.** A tarball of the directory ships the 4 KB
  `memory.db` shell and leaves 2.5 MB in the `-wal` — unless snapshot exports
  through SQLite, which running inside the application does make easy.

`scp` also bypasses the backend interface, requiring both machines reachable at
once.

**What the proposal is right about**, both points against the design chosen here:

- **A first-party command versions with the schema.** This tool is outside-in: it
  infers merge policy from column names and refuses on schema drift because it
  cannot migrate. A command shipped with KiroCrew would carry KiroCrew's own
  migrations, and "schema drift blocks sync" would stop being a limitation and
  become a handled case. This is the strongest argument for eventually moving
  sync into KiroCrew core — where the deferred operation log needs to go anyway.
- **There is no seeding primitive.** `init` scaffolds config only. Re-seeding is
  named as the recovery path twice above — after the wire-format change, and
  after the third-machine push race — with no command behind it. Component
  scoping chosen at call time is likewise something a static allowlist does not
  give.

Both are worth having as a follow-up, built on the existing pipeline rather than
beside it:

```
kirocrew-sync.sh export --components memory,artifacts -o snap.tar.gz
kirocrew-sync.sh import snap.tar.gz --mode replace
```

`export` is `unpack` plus `tar`, with the §6 gates already applied on the way
out. `import --mode replace` is the missing seed. There is deliberately **no
`--mode merge`**: merging is what `sync` does, with a base, and offering a
baseless merge beside it would be offering a subtly broken one.

## Verification

`tests/run_tests.sh` runs two simulated machines against a local-directory
backend: 59 assertions across 12 scenarios covering disjoint edits, same-row
conflicts, delete-vs-edit, delete propagation, both override strategies,
machine-local state isolation, credential containment, structural config merge,
session union, FTS rebuild over rows received from the other machine, event-log
renumbering, convergence under repeated idle syncs, quarantine of a mismatched
machine and its self-healing once the models match, and dry-run safety.

Two of those assertions exist because the suite was previously blind to their
failure. Exit codes are asserted, because the EXIT trap referenced an
out-of-scope local and every successful sync exited 1 unnoticed. And the
credential scan unbundles before grepping, because grepping the bundles
directly searched zlib-compressed packfiles — it would have passed whether or
not the secret was published. The scan now also asserts it can see known
content, so it cannot go vacuously green again. `tests/test_sync_paths.sh` covers path portability through
the new pipeline, and the ADR 0001 suites (`test_portable_paths.sh`,
`test_config_precedence.sh`) still pass unchanged.
