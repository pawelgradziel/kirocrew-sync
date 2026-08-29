# Upstream sync review — KiroCrew v0.5.0-insider.3

- **Date:** 2026-08-29
- **Reviewed against:** `kirodotdev/KiroCrew` tag `v0.5.0-insider.3`
  (commit `0e9465f`, `__version__ = "0.5.0"`)
- **Question:** move forward with this repo's sync engine, or reuse KiroCrew's,
  for sessions / knowledge / memory across devices?
- **Answer:** move forward. There is no upstream sync to reuse.

## Method, and why the tag matters

Read at the **released tag**, not `main`. `main` is at `__version__ = "0.6.0"`
— unreleased staging code whose behaviour no user has. Every claim below was
checked in the v0.5.0-insider.3 tree; where a fact came from the changelog or
the issue tracker it is cited as such.

## What upstream actually has

| Mechanism | What it does | Why it is not device sync |
|---|---|---|
| `snapshot.py` (3.9k lines) | Local snapshot/restore of 7 components. New in 0.5.0: restore-into-live-state, `replace` or `merge`, with a rollback ledger. | Merge is **baseless**. `_merge_memory` is `INSERT OR IGNORE` over 4 tables; files are copy-if-absent. No ancestry, so deletes never propagate and concurrent edits are never detected. `knowledge.db` is **replaced, not row-merged** (its own component help says so). |
| `portability.py` | Zip export/import over HTTP for the dashboard. | One-shot state transfer. Same baseless-merge problem, file-granular. |
| AWS Control app | Nightly push of a snapshot tarball, plus a sessions tarball, to S3. | One-way backup. Its own module docstring: *"**Restore is a download, deliberately.** A restore lands the archive in `<app data dir>/restore/` and hands back the path; nothing hot-swaps a live `memory.db` or sessions dir under a running gateway."* |
| `sync_bridge.py` | Dashboard ↔ Slack session handoff by symlinking the JSONL. | Same machine. Not transport. |
| `knowledge/sync.py` | Ingestion sweep for knowledge *sources*. | Local ingest state, unrelated to cross-machine sync. |

**0.5.0 moved away from us, not toward us.** From its "Before you upgrade":

> **Snapshot-to-S3 is retired** — `kirocrew snapshot --to s3://…`,
> `--aws-profile`, and the `s3://` fetch path are gone; cloud backup moves to
> the AWS Control app. Snapshots gain restore-into-live-state (replace or
> merge, with a rollback ledger) in exchange.

Upstream deliberately **removed the remote transport from `snapshot`** and
replaced it with a nightly one-way backup in an app. Verified in the tree: no
`boto3`, no `s3://`, no upload path left in `snapshot.py`.

## What upstream does not have

Cross-device sync is an **open, unassigned feature request with no linked PRs**:

| Issue | Title | State |
|---|---|---|
| #6223 | Multi-machine memory sync / shared workspace across devices | Open, unassigned, `needs-human` |
| #4923 | Add first-class Kiro Cloud Session support | Open, unassigned, `needs-human` |
| #6340 | Isolated memory containers: scope memory to a workspace or group of projects | Open, unassigned |
| #3278 | Hybrid local/remote execution: offload to a persistent remote instance | Open |

#6223 states the current model in upstream's own words: *"Remote Crew lets the
MacBook connect as a client, but the mental model is 'one brain, one machine'."*

A PR search for snapshot/restore/merge/backup work returned nothing in flight.

**Their strategic direction is federation, not sync**: 0.4.0 shipped session
search across all connected gateways and an opt-in MCP set for one session to
message and read another. That is live access to one brain from many places —
it does not make a second machine work offline, and it does not converge state.

## Verdict

Reusing upstream's merge would be a **downgrade**, and for the exact reasons
[ADR 0002](adr/0002-three-way-sync-via-unpacked-git-repo.md) already recorded
on 2026-08-06 under *"Snapshot + restore as a KiroCrew subcommand"*. That
analysis was re-checked against v0.5.0-insider.3 and still holds:

- **`--mode merge` still has no base.** 0.5.0 added a rollback ledger, which
  makes restore *recoverable*, not *convergent*. It still cannot tell "I added
  this" from "they deleted this".
- **The empty-database bug is still live.** Both `vector_memory.py` and
  `knowledge/store.py` still `PRAGMA journal_mode=WAL`, so a file-level copy
  still ships a stub while the content sits in the `-wal`.
- **Component scoping still cannot reach credentials inside files.**
  `config.json` still carries `bot_token` beside real settings.
- **A tarball still carries this machine's absolute paths**, so it cannot
  deduplicate a folder added under different paths on two machines.

What is genuinely worth **taking** from upstream, later:

1. **`snapshot.py`'s rollback ledger** (`_allocate_rollback_dir`, phase-one/
   phase-two split) is a good pattern for the `import --mode replace` seeding
   primitive ADR 0002 lists as missing. Take the shape, not the merge.
2. **Their component table is a checklist.** `crons.json`, `notifications.jsonl`
   and `hooks.json` are in their backup set; only `hooks.json` is in our `ALLOW`.
   Worth a deliberate decision per file rather than an omission.

## Drift found against v0.5.0-insider.3, and what was done

Three weeks of upstream change since ADR 0002. Checked table by table.

**Still correct, verified:**

- `memory.db` — all five upstream tables (`semantic_memory`,
  `episodic_memories`, `memory_events`, `memory_meta`, `schema_version`) have
  explicit policy. No new tables.
- `memory_meta.embedding_space_sig` is still the key upstream writes
  (`vector_memory._EMBED_SIG_KEY`), so the embedding gate still fires.
- Sessions still live at `<data home>/sessions/*.jsonl`.

**Fixed in this change:**

1. **`agent_item_state` was uncovered.** KiroCrew 0.5.x added it to
   `knowledge.db`; it fell through to `policy.infer()`. Inference happened to
   be safe — LWW on `updated_at`, withheld from team scope — which is ADR 0002
   §6 working as designed. Now explicit, and `shared=True` to match its twin:
   upstream documents it as *"Same shape and role as `artifact_item_state`"*,
   its `slug` is content-derived rather than a filesystem path, and `sources`
   and `items` already travel — withholding only the per-document state would
   leave a colleague unable to remove the items they did receive.
   Every upstream `knowledge.db` table now has explicit policy.

2. **Session archive segments did not sync.** KiroCrew rolls the older turns of
   a long conversation out of the live transcript into
   `sessions/archive/<key>__<stamp>.jsonl`. `ALLOW` had `sessions/*.jsonl`,
   which is deliberately non-recursive — so the live half of a conversation
   crossed machines and its older half did not, and the same session read as
   truncated on the second machine. Upstream names this invariant explicitly in
   the AWS Control backup module: *"one tarball of BOTH session halves … the
   'both halves move together' invariant is honoured by construction."* Ours
   was not. `sessions/archive/*.jsonl` is now allowlisted with the same `union`
   merge driver, and stays out of team scope like every other transcript.

**Found, not fixed — recorded:**

3. **The CLI half of a session is outside the sync root.** Upstream backs up
   *two* session directories: `<data home>/sessions/` (ours) and
   `<kiro home>/sessions/cli/` — the kiro-cli replay logs, which live at
   `~/.kiro/sessions/cli`, **outside** `~/.kiro/crew`. `KIROCREW_DIR` cannot
   see it, so a session's crew transcript syncs and its CLI replay log does
   not. Closing this needs a second sync root, not an allowlist entry.

## Recommendation

Keep building here. Upstream is not going to solve this soon: the request is
unassigned with no PRs, and 0.5.0 actively retired the one transport it had.

The two follow-ups ADR 0002 named remain the highest-value next steps, and
upstream's 0.5.0 work makes the first one easier rather than redundant:

- `export` / `import --mode replace` as the seeding primitive (no `--mode
  merge`; merging is what `sync` does, with a base).
- Contribute the design to #6223 if a first-party home is ever wanted — the ADR
  itself argues that sync eventually belongs in KiroCrew core, where it would
  carry KiroCrew's own migrations and schema drift would stop being a
  quarantine and become a handled case.

## Verification

All suites green after the changes, on this branch:

| Suite | Before | After |
|---|---|---|
| `tests/run_tests.sh` | 59 | **61** passed, 0 failed |
| `tests/test_team_scope.sh` | 29 | **33** passed, 0 failed |
| `tests/test_portable_paths.sh` | 40 | 40 passed |
| `tests/test_sync_paths.sh` | 11 | 11 passed |
| `tests/test_config_precedence.sh` | — | passed |
| `tests/test_backend_local_list.sh` | — | passed |

The six new assertions cover both directions of the archive fix: that archived
turns reach the other machine in personal scope, and that they reach neither
the colleague nor the published bundles in team scope.
