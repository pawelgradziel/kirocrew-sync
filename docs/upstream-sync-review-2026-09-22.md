# Upstream sync review — 2026-09-22 (session sharing)

Follow-up to the weekly "KiroCrew cloud sessions & chat sharing watch" routine
(baseline 2026-09-05), checked against `kirodotdev/KiroCrew` `main` at
`f7b6e6fb` (2026-09-22) and tag `v0.7.0-insider.6`. Two questions:

1. Did upstream change how a chat session is shared?
2. Can kirocrew-sync reuse any of it?

## 1. What changed upstream

**Verdict: partially.** A session can now leave one machine as a file and be
installed on another, including someone else's. There is still no hosted
session, no share link, and no multi-user identity. KiroCrew is still a
single-operator tool.

| Change | PR | Merged | What it does |
|---|---|---|---|
| Export one session to a file | #9685 | 2026-09-10 | `GET /api/chat/slots/{slot}/export` downloads `<title>-<stamp>.kcsession.json.gz`. Refuses incognito and temporary sessions. |
| Import a session from a file | #11315 | 2026-09-17 | `POST /api/chat/slots/import` takes that file (or the plain-JSON tunnel bundle; it sniffs gzip from the first two bytes). New sidebar row "Import a session from a file". |
| Export can carry Layer B | #11297 | 2026-09-18 | `dashboard.export_include_layer_b` (off by default) puts the kiro-cli context window into the file, so an import resumes via `session/load` instead of replaying a text prefix of the history (80K chars, `context._REPLAY_BUDGET_CHARS`; upstream docstrings still say ~8K). |
| Scrub provenance from exports | #11607 | 2026-09-18 | Host and login details are removed from the downloadable bundle. |
| Arrival provenance filing | RFC #12189, #12196 (closes #11468) | 2026-09-21 | Every arriving session, from a file or pushed from a peer, is filed under `Imported / from <sender>`. |

The CHANGELOG lists this under 0.7.0 as "Sessions travel as files". Tunnel push
between your own instances (instances spec §14, "Send a copy to ▸ instance")
already existed; the file route is what's new, and it works between two
machines that are never online together, or that belong to different people.

**Unchanged:** #4923 (Kiro Cloud sessions), #3278 (hybrid local/remote), and
#7577 (`session move`) are all still open, unassigned, `needs-human`, with no
comments or linked PRs since 2026-09-05. The chat-core seam (#8690 RFC, #8689
transport) merged on 2026-09-05. A first-page search of open issues and PRs
found nothing new about sharing, multi-user, or session sync; later pages were
not read. Nearby but not the same thing: #12821 (remote executor placement,
open) and Fargate crews under `src/kiro_crew/cloud/`, which are self-hosting.

"Copy, never move" is a stated rule in the bundle: an import always creates a
new session and never touches an existing one. A file shared with a colleague
is therefore a **fork**, not a shared conversation. Nothing flows back.

## 2. What kirocrew-sync takes from it

### Fixed here: `session_map.json` no longer syncs

The upstream transfer code and spec (`session_transfer.py`, instances.md §14.1a)
spell out a relationship this repo had not accounted for:

- `session_map.json` maps a session key to a kiro-cli **sid**. The context
  behind that sid lives at `kiro_sessions_dir()/<sid>.{json,jsonl}` =
  `~/.kiro/sessions/cli`, which is outside our sync root (TODO: "Sync the
  kiro-cli half of a session").
- `SessionMap.prune()`, run at gateway startup, **deletes every entry whose
  `<sid>.json` is missing**. `SessionMap.get()` does the same on read.
- Upstream's own portable export (`portability.EXPORT_EXCLUDE`) and the
  session bundle both treat the map as machine-local.

We had it in `ALLOW`, so this happened:

1. Machine A syncs, and its entries reach machine B.
2. B's gateway starts, finds no CLI files for A's sids, and prunes them.
3. B syncs. The structural JSON merge sees "B deleted, A unchanged" and keeps
   the deletion.
4. A pulls and loses **its own** resume mappings. Its sessions fall back to the
   lossy history prefix, even though the context files are still on A's disk.

Channel-bound entries are kept with their `sid` cleared, and that clearing
spreads back to A the same way.

`session_map.json` has moved from `ALLOW` to `DENY` in `lib/kcsync/files.py`.
Existing installs are safe: `pack` never deletes local files, so each machine
keeps its own map. The next `unpack` just drops the stale copy from the sync
repo. Covered in `tests/test_sync_paths.sh`.

What stops travelling with it: per-session agent and project overrides, and
durable flags. Channel bindings stop travelling too, which is correct, since
two gateways should not both answer the same Slack thread. Crew transcripts
(`sessions/*.jsonl`) still sync, so the conversation is still readable on every
machine.

### Recorded for the CLI-half TODO: constraints upstream has measured

Anyone picking up "Sync the kiro-cli half of a session" should start from these.
All of them are in `session_transfer.py`:

- **Byte-exact or broken.** The CLI envelope carries thinking blocks with a
  provider signature over their content. Rewriting any covered byte makes
  `session/load` succeed and the *next turn* fail. Upstream measured that a
  redaction pass broke a signature in 41% of 704 real sessions. The signed
  content sits in the envelope `.json` (`session_state.conversation_metadata`),
  not only the `.jsonl`. So neither file may go through `merge=union`, the
  `kcsync-json` merge, or `strip_secrets`. What must be preserved is the signed
  content, not the file bytes: upstream itself re-serialises the envelope.
  Two machines appending to the same sid is a conflict to keep as two copies,
  not something to merge.
- **Machine-specific fields.** `_rewrite_layer_b_envelope` lists them:
  `session_id`, `cwd`, and
  `session_state.permissions.filesystem.allowed_{read,write}_paths`. Upstream
  deliberately *clears* `cwd` and the path lists rather than translating them,
  matching its choice to drop `project`. Map entries carry a machine-specific
  `cwd` too.
- **The join is on-loop.** Upstream writes the two files off-loop and does the
  map join on the gateway's event loop. The map now has a process-level lock
  (`_MAP_LOCK`), but its threading contract still requires every write to go
  through the live map: a second process's whole-file save loses entries. Our
  running-guard (`check_kirocrew_running`) does **not** protect against this.
  It lets pack run while KiroCrew is up but idle, judged only by recently
  changed `*.db` files.

### Reuse option (not built): sync as a mailbox for session bundles

Upstream says the file route exists because a tunnel needs both machines up at
once (§14.7). kirocrew-sync backends (S3/R2, rsync) already provide the
asynchronous mailbox that route is missing. A thin `send-session`/`inbox`
command could put `.kcsession.json.gz` files on the backend and install them on
the other side with `POST /api/chat/slots/import`. That gets upstream's
validation, redaction, size caps and `Imported` filing. The file export sets
`origin=""`, so file imports land under `Imported` directly, not
`from <sender>`. Limits: exporting Layer B needs the owner dashboard, and an app
token gets neither Layer B nor slots it does not own
(`is_owner_dashboard_request`). Each import is a new session with no stable
source id, so the receiver needs its own dedup ledger. The copy's transcript is
then synced back to the sender like any other transcript. Sending transcripts
to a colleague conflicts with ADR 0002's team scope. The decision is in
[ADR 0003](adr/0003-kiro-cli-session-half.md): `sync` does not carry the CLI
half, and a separate, explicit personal-scope `send-session`/`inbox` verb does.

### Fixed here (personal scope): member memory stores now sync

v0.7.0 added isolated per-member memory (#9553, simplified to one SQLite store
per member in `bc0d2b0cf`) under `<data home>/memory_stores/`. That path was
not in `ALLOW`, and `DATABASES` in `policy.py` only knew `memory.db` and
`knowledge.db`, so crew-member lessons and memories stayed on the machine that
learned them.

**Layout, from upstream `memory_stores.py`, `memory_schema.py`,
`vector_memory.create_member_database` and `member_memory_backup.py`:**

| Path under `memory_stores/` | What it is | Here |
|---|---|---|
| `<name>/memory.db` | The memory. A V2 member store holds `memory_items` (facts, directives/lessons and episodes in one table, with embeddings), `memory_events`, `memory_meta`, `schema_version`, `memory_record_meta`, `memory_revisions`, and the member tables `member_database` (identity), `memory_history`, `memory_consolidations`, plus the `memory_fts` FTS5 table and two read-only views. A named V1 store's `memory.db` is the same crew lineage without the member tables. | Rows, as logical database `memory_stores/<name>` |
| `<name>/memory/preferences.md`, `projects.md` | Manual documents | File |
| `<name>/memory/history/*.md` | A named V1 store's daily history | File |
| `<name>/lessons.jsonl` | A named V1 store's lesson file | File |
| `<name>/member-memory.json` | Legacy ownership manifest, read by the upgrade | File |
| `<name>/memory_index.db`, and `memory_fts` inside `memory.db` | Derived FTS | Never synced; `memory_fts` is rebuilt after pack using upstream's `rebuild_memory_index` derivation |
| `.member-api-key`, `.member-backups/`, `.execution-logs/`, `<name>/backups/` | Host-local (`is_host_local_store_state`) | `DENY` |

**What changed:**

- `lib/kcsync/stores.py` finds stores on disk and in the sync repo. It
  validates each name with upstream's rules (one lowercase segment, no dots or
  separators, not `default`, not a Windows device name). It also skips a store
  whose directory resolves anywhere but itself, or whose `memory.db` is a
  symlink or hard link. These are the same identity checks upstream applies,
  so a link from `acme` to `finance` cannot merge two members' memory.
- Each store is its own logical database with its own `_schema.sql`, so the
  drift gate and the pre-merge quarantine work per store. The embedding gate
  compares each store's own `embedding_space_sig`. A store on both machines
  with two signatures is an error, so that machine is quarantined. A store that
  exists only on the remote gets a warning: it arrives as its own file, and
  KiroCrew reconciles its embedding space when it opens the store.
- A store that exists on only one machine is created on the other from
  `_ddl.json`, which lists every CREATE statement including the FTS table and
  the views. The directory is `0700` and the file is `0600`, as upstream
  creates them. If the pack fails, the new file is removed.
- Store tables have explicit overrides in `policy.py` (`memory_stores/*`).
  `memory_events` and `memory_revisions` are unioned on their natural key and
  renumbered. `memory_items` uses LWW on `updated_at`, never on
  `last_accessed_at`. `member_database` travels, because a store without it is
  refused; its `(member_id, store_id)` is not machine-specific. `memory_fts` is
  skipped.
- `.gitattributes` now uses `db/**/` for `_schema.sql`, `_policy.json` and
  the new `_ddl.json`. The merge driver resolves two-segment database names.
- Covered by `tests/test_member_stores.sh`: a store appearing on the other
  machine, row merges, renumbering, LWW, host-local files never published,
  schema and embedding quarantine, invalid names and aliased stores, and team
  scope withholding.

**Deliberately excluded:**

- **Team scope: all of it.** Upstream builds this memory to be private to one
  member. A member's lessons are `memory_items` rows in the same table as raw
  episodes, and the engine publishes whole tables, so the lessons cannot be
  shared without the episodes. Team scope does not scan `memory_stores/` at all,
  because even a store name discloses a member id.
- **Host-local state**, as listed above. A restored backup from another machine
  would roll this machine's store back to that machine's past. `.member-api-key`
  is a historical credential.
- **Nothing was excluded because of a machine-bound identity.** Upstream's
  routing reads `member_database` and `config.json`, and neither contains a
  host key. The only key file, `.member-api-key`, is already host-local.

**What remains (not fixed):**

- **Deleting a whole store does not propagate.** A store directory removed on
  one machine is created again from the repo on the next pack. Upstream keeps
  the stores of deleted members, so this should be rare.
- **Two machines writing history for the same day.** `memory_history` is one
  row per day, rewritten on every append. If both machines append to the same
  day, LWW keeps one machine's version and logs a conflict.
- **The same member created on two machines before they sync.** Each machine
  allocates its own `member_id` and store name. The key-by-key `config.json`
  merge can then take `memory_store` from one machine and `member_id` from the
  other. KiroCrew would refuse that member until it is fixed by hand. Both
  stores still sync.
- **Found while doing this: the global `memory.db` has the same revision
  journal.** Upstream runs `memory_record_metadata.ensure_schema` on every
  vector file, so the root `memory.db` also has `memory_revisions` (an
  AUTOINCREMENT id) and `memory_record_meta`. `OVERRIDES["memory"]` does not
  list them yet. `memory_revisions` is therefore inferred as LWW keyed on the
  machine-local id, so revision 5 on two machines can collide. The fix is to
  copy the two store overrides into `OVERRIDES["memory"]`. It is not in this
  change.

## Recommendation

Upstream is not building session sync or shared sessions, so the verdict from
earlier reviews stands: keep building here. For the session half specifically,
prefer upstream's bundle format and import route over copying CLI files by hand.
It is versioned (`bundle_version` 2, with additive keys) and already handles the
signature and path problems above.

## Follow-up: other prune-and-merge-back hazards

The `session_map.json` bug has a general shape. KiroCrew removes or rewrites
an entry because of something only true on one machine, and the three-way
merge then treats that as an ordinary one-sided edit and applies it everywhere.
Two engine properties decide where this can happen:

- `merge_json` keeps a one-sided key deletion, and treats a JSON **list** as a
  single value. So a rewrite of any list on one machine replaces the untouched
  list on the other.
- Whole-file deletions do *not* spread: `pack` never deletes a local file. A
  file one machine deletes comes back from any machine that still has it. That
  is a separate issue (resurrection, not loss), and none of the cases below
  depends on it.

Every file in `ALLOW` was checked against upstream `main` for writers that
remove or rewrite entries on their own, meaning at load, at startup, or in a
sweep, rather than because the user asked.

### Fixed

| File | Upstream writer | Hazard | Fix |
|---|---|---|---|
| `autonudge.json` | `autonudge.py` `AutoNudgeService._load`, `repair_sentinel_path` | Every gateway re-arms the loops it finds, so a synced store fired the same nudges on every machine, which is the `crons.json` problem. `_load` also moves rows that fail *this host's* credential policy into `autonudge.quarantine.json` (not synced), stops loops interrupted mid-delivery, and re-homes or clears `stop_sentinel_path` against the local data home. `loops` is one list, so the rewrite replaced the owner's copy. | `DENY` (`lib/kcsync/files.py`) |
| `config.json` (`connections_ui`) | `config/loader.py` `load` / `_apply_document_migrations` | A stored `connections_ui: false` is stripped unless the local marker `connections_ui_migrated.json` exists. A machine without the marker (a newly onboarded one, say) deleted a deliberate opt-out, and the deletion merged back. | Marker added to `ALLOW` |
| `config.json` (superseded defaults) | `config/superseded_defaults.py` `auto_adoptable`, `record_adoptions`; `loader.py` migration | `agent.chat_turn_timeout_secs` = 7200 and `agent.subagent_timeout_secs` = 1800 are removed once, when the ledger `superseded_acked.json` does not list them. Upstream's own docstring says being removed twice is "precisely what the one-shot guarantee exists to prevent". Without the ledger, a second machine removed a value the operator had restored, and that removal merged back. | Ledger added to `ALLOW` |
| `config.json` (`memory.embed_model_stamp`, `memory.embed_model_legacy_ids`) | `embeddings.py` `_verify_custom_model`, `_model_file_stamp`; the legacy-ids reconcile in `embeddings.py` | The stamp is `stat()` of the local model file (`st_dev`, `st_ino`, `mtime_ns`, …), so it never matches on another machine. Each machine re-hashed the weights and wrote its own stamp, producing a conflict on every sync. On a machine whose vectors predate the recorded model file, a foreign stamp fails the legacy-vector check and starts an embedding-space change, which means a full re-embed. The legacy ids can also be popped by the rewrite. | New `LOCAL_ONLY_KEYS` in `files.py`: stripped on unpack and restored from the local file on pack, the same way secrets are handled |

All four fixes are covered in `tests/test_sync_paths.sh`. The tests also check
that a repo written by an older build cannot plant another machine's values.

### Found, not fixed

- **List-valued files lose one side wholesale** (`tags.json`,
  `tag_boards.json`, `hooks.json` `hooks`). When both machines change the list,
  `merge_json` keeps one of the two lists. Upstream then makes the loss stick.
  `DashboardState.load_tags` prunes column `tag_ids` that are not in the
  vocabulary. The slot-restore paths in `dashboard/chat_persistence.py` prune
  each chat's `tags`, which live in the transcript's metadata line, against the
  same vocabulary, and the next save writes the pruned list back. `load_tags`
  also seeds a default vocabulary when `tags.json` is missing, which gives a
  fresh machine a competing list. Script hooks write
  `last_run`/`run_count`/`last_status` on every run, so the `hooks` list
  changes constantly. Not fixed here because the fix is a merge rule: merge
  lists of `{id: …}` objects by id, with an ordering that does not depend on
  which side is "ours" (both tag files carry an `order` field). That changes
  merge semantics for team scope as well, so it deserves its own change and
  tests.
- **App trust grants** (`config.json` `agent.apps_trusted`,
  `apps_trusted_local`, `apps_trusted_repositories`). `apps/**` is denied, so
  apps are per machine, but the grants sync. `apps/manager.py`
  `_drop_trust_grant`, run on uninstall, therefore withdraws the grant on every
  machine, including one that still has the app. This fails closed and is
  fixed by re-trusting. The reverse direction is the more serious one: a
  name-only grant can reach a machine where the same name is a different app.
  Registry grants are bound to a repository, but local grants are bound only to
  the name. Not fixed because whether consent should follow a person across
  machines is a product decision. `LOCAL_ONLY_KEYS` would implement either
  answer.
- **`admission_policy.json` checksum.** Nothing upstream prunes it. But `pack`
  rewrites every JSON file key-sorted, so the bytes stop matching the seed
  checksum in `.migrations/admission_policy.sha256` (local and denied), and
  `platform/admission.py` `_verify_seed_integrity` logs an integrity event on
  every load. This is detection only; the policy itself is not affected.
  Skipping the write when the parsed content is unchanged would fix it.
- **App history classifier** (`app/backend/artifacts.py` `_classify_path`).
  It matches file names against `record.table`, but for JSON conflicts
  `merge_json` records the JSON key path there (for example
  `dashboard.theme`), and the file path is in `record.path`. So real
  config-file conflicts are never classified. The unit test builds records
  with the file name in `table`, which is why it passes. `session_map.json` and
  `autonudge.json` stay in `_CONFIG_FILE_NAMES` (the comment there explains
  why), but the classifier should read `record.path`.

### Checked, no hazard

- `model_windows.json` (`model_registry.py` `refresh_kiro_windows`,
  `persist_kiro_windows`): add or update only, and never removes an entry.
- `sessions/*.jsonl` and `sessions/archive/*.jsonl` (`history_rewrite.py`
  `_rewrite_session_locked`, `_maybe_rotate`;
  `channel_transcript_migration.py`): compaction and rotation archive every
  dropped row into `sessions/archive/`, which syncs. The channel migration is
  a content merge followed by a file delete, and file deletes do not spread.
- `artifacts/**` (`artifacts.py` `prune_auto_widgets`, `_prune_versions`,
  `_prune_oldest_threads`): the sweeps delete whole directories or files,
  which do not spread. The comment cap works on synced content, not local
  state. `clear_publication` and `mark_webapp_expired` run only on user
  actions.
- `workspace/*.md`, `workspace/memory/**` (`memory.py` `prune_history`):
  date-based file deletion, not machine-local.
- `hooks.json` webhook contexts (`mcp_tools/control.py` `register_hook`,
  `hooks.py` `_write_hooks_file`): additive, and nothing expires or deletes
  them on disk.
- `config.json`, other startup writers: the workspace, agent seed and
  default-agent migrations, the `meta.lastTouchedVersion` stamp, and
  `memory_stores.py` `migrate_legacy_member_stores` only add or fill values.
