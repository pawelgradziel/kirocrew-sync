# ADR 0003: The kiro-cli half of a session is not synced; resumable copies are sent, not merged

- **Status:** Proposed
- **Date:** 2026-09-22
- **Deciders:** kirocrew-sync maintainers
- **Checked against:** `kirodotdev/KiroCrew` `main` at `f7b6e6fb` (2026-09-22).
  Every upstream fact below is cited as `file:symbol` and was read in that
  tree, not taken from its docs.
- **Relates to:** [ADR 0001](0001-knowledge-path-portability.md) (path
  portability), [ADR 0002](0002-three-way-sync-via-unpacked-git-repo.md) (the
  sync pipeline), TODO "Sync the kiro-cli half of a session",
  [upstream-sync-review-2026-09-22](../upstream-sync-review-2026-09-22.md)

## Context

A KiroCrew chat session is stored in two places, and only one of them is inside
the sync root.

| Half | Where | Owner | Synced today |
|---|---|---|---|
| **Layer A**: the display transcript | `<data home>/sessions/<key>.jsonl` + `sessions/archive/<key>__<stamp>.jsonl` | KiroCrew | yes, `merge=union` |
| **Layer B**: the model's context window | `kiro_sessions_dir()/<sid>.json` + `<sid>.jsonl` = `~/.kiro/sessions/cli` | kiro-cli | no, it is outside `KIROCREW_DIR` |
| **The join**: session key to sid | `<data home>/session_map.json` | KiroCrew | no, `DENY` since 2026-09-22 |

(Upstream names the layers in `session_transfer.py` module docstring and
`instances.md` §14.1a. `config/paths.py:kiro_sessions_dir` resolves Layer B
lazily from `KIRO_HOME`, so it can move independently of the data home.)

When a machine has Layer A but no joined Layer B, the next turn cannot use
`session/load`. It rebuilds context from the transcript instead
(`dashboard/chat_persistence.py:_build_history_prefix` →
`context.py:build_session_replay`). That replay keeps only recent
`user`/`assistant` text within `_REPLAY_BUDGET_CHARS` (80,000 chars, scaled
down to 20% for small model windows). It carries no tool state and no
compaction state. So today a session continued on a second machine is readable
there but resumes with less context.

The question is whether kirocrew-sync should move Layer B, and how.

### Upstream facts that constrain every option

1. **Layer B must be copied exactly.** Thinking blocks inside the envelope's
   `session_state.conversation_metadata` carry a provider `signature` over
   their content, and the provider validates it on replay. If any signed byte
   changes, `session/load` still succeeds but the *next turn* is rejected.
   Upstream measured this: a leaf-string redaction pass altered a signature in
   **41% of 704** real sessions (`session_transfer.py:_read_layer_b`,
   `security_posture.py` "Session transfer bundle" row, `instances.md`
   §14.1a). Upstream therefore validates inbound events by parsing them only,
   and stores the original string
   (`session_transfer.py:_events_jsonl_is_loadable`).
   What must not change is the signed *content*. The envelope's file bytes
   can change: `_write_layer_b_files` re-serialises the envelope with
   `json.dumps` after `_rewrite_layer_b_envelope`.
2. **Some envelope fields are machine-specific.**
   `session_transfer.py:_rewrite_layer_b_envelope` rewrites `session_id` to a
   fresh uuid. It clears `cwd` and
   `session_state.permissions.filesystem.allowed_{read,write}_paths`, sets
   `agent_name` to the target's agent, refreshes the timestamps and drops
   `title`. It **clears** the paths rather than translating them, which
   matches the transfer's decision to drop `project`. Map entries carry a
   machine-specific `cwd` too (`session_map.py:SessionMap.set`).
3. **The join must go through the live map.** `SessionMap` rewrites the whole
   file from its in-memory `_data` on every mutation. A process-level
   `_MAP_LOCK` orders access within one process. Rule 3 of the
   `SessionMap` threading contract says writes must still go through the
   *live* map. A detached instance's entry is dropped by the next unrelated
   write, and "two gateways writing one map file would still lose updates"
   (`session_map.py:SessionMap` docstring,
   `session_transfer.py:_join_layer_b`). Upstream writes the two files in a
   worker thread and does the join on the gateway loop through
   `SessionManager.seed_conversation`.
4. **Entries are pruned when their files are missing.**
   `session_map.py:SessionMap.prune` runs at startup
   (`session_pool.py:start_pool`). It deletes an entry whose `<sid>.json` is
   missing, or clears the entry's `sid` when a channel binding, a durable flag
   or a generation floor must survive (`_survives_prune`).
   `SessionMap.get` does the same on read, and also treats a `<sid>.jsonl`
   under 10 bytes as stale. A join that arrives before its files, or without
   them, is destroyed on the receiving machine.
5. **Size caps.** A bundle is limited to 5,000 messages, 1,000,000 chars per
   message and 20,000,000 chars of transcript
   (`_MAX_MESSAGES`, `_MAX_CONTENT_CHARS`, `_MAX_TOTAL_CHARS`). Each Layer B
   file is limited to 40,000,000, checked against `st_size` before the read
   (`_MAX_LAYER_B_CHARS`, `_read_layer_b`). A gzip body may expand to at most
   `_MAX_DECOMPRESSED_BYTES`. The gateway body limit is 60 MiB
   (`_GATEWAY_CLIENT_MAX_SIZE`, `dashboard/server.py` `client_max_size`). If a
   Layer B file is over its cap, the sender leaves Layer B out and sets
   `layer_b_skipped` (`_read_and_assemble`). The receiver then marks the tab
   "transcript only".
6. **The Layer B store is large and has local churn.** One measured machine
   has "half a million replay files". A subagent run leaves a replay log with
   no transcript. Reclaiming a session moves both halves to the trash
   together (`session_storage.py` module docstring). A rewind deletes the
   orphaned CLI pair (`dashboard/chat_rewind.py:_delete_orphan_kiro_session`).
   The store can be shared with older pods (`session_storage.py`, "Sessions
   this instance does not own").
7. **Import always creates a copy.** `POST /api/chat/slots/import` always
   mints a new slot key and a new sid, and "needs no idempotency key"
   (`session_transfer.py:api_chat_slot_import`, `instances.md` §14.2). The
   bundle has no stable source-session identity. The file route even sets
   `origin=""` (`session_export.py:api_chat_slot_export`), so a file import is
   filed under `Imported` directly, not `Imported / from <sender>`
   (`arrival_folders.py:sender_folder_name`).

### kirocrew-sync facts that constrain every option

- Team scope never publishes transcripts. `TEAM_ALLOW` in
  `lib/kcsync/files.py` does not include `sessions/**`, and ADR 0002 §6 makes
  team scope an allowlist.
- `pack` never deletes local files (`files.py:pack_files`).
- `check_kirocrew_running` in `kirocrew-sync.sh` lets a pack run while
  KiroCrew is **running but idle**. Idle means no `*.db` written in the last
  minute. It does not look at `session_map.json` or `~/.kiro/sessions/cli`.
  So "safe only while KiroCrew is stopped" is **not** what the guard enforces.
- `.gitattributes` in the sync repo sends `files/**/*.json` through the
  structural JSON merge. Unpack also rewrites every `.json` pretty-printed
  with sorted keys, after `strip_secrets`. The whole tree is `text=auto
  eol=lf`. Layer B files must stay out of all three.
- `sessions/*.jsonl` is `merge=union`. When a conversation is continued on two
  machines, its turns are concatenated line by line.

## Decision drivers

- Never corrupt a resumable context. A broken signature fails on the turn
  *after* it is loaded, far from the cause (fact 1).
- Never publish Layer B in team scope. It is an unredacted context window.
- Do not write KiroCrew-owned state that the running gateway rewrites from
  memory (fact 3).
- The two halves must not disagree after a sync. If a join points at a missing
  file, or a CLI log belongs to a transcript that was merged, the session is
  worse than transcript-only.
- Prefer upstream's format and code paths over copying upstream's internals.

## Options considered

### Option A: a second sync root for raw CLI files ✗ Rejected

Add a `KIRO_HOME`-derived root with its own allowlist. `<sid>.json` and
`<sid>.jsonl` sync as opaque whole files (`binary`, `-text`, never line-merged
or JSON-normalised), and something else carries the join.

**What carries the join.** `session_map.json` is `DENY`'d, and it should stay
that way (see the 2026-09-22 review). A replacement would be a sidecar that
unpack derives read-only from the local map, such as
`cli/index.json: {session key → sid}`, holding only the sids that are joined
to a synced transcript. Walking all of `sessions/cli` would try to publish
half a million subagent logs (fact 6). On the receiving side, pack would have
to create entries in the local map. It cannot do that safely:

- The guard lets pack run while the gateway is up (see above), and the
  gateway's next whole-file write drops an entry added from outside (fact 3).
  Pack would need a stricter "KiroCrew fully stopped" guard just for this
  step. That is exactly the constraint the dashboard app cannot meet: the
  existing comment in `check_kirocrew_running` says the app "only ever runs
  with KiroCrew up".
- Upstream has no route that only seeds a join. `seed_conversation` is called
  from subagent continuation and from `_join_layer_b` inside the import
  handler, not from any endpoint.
- If the index merges ahead of its files (or a file is too big to sync),
  startup `prune` deletes the entry locally (fact 4). That is now contained,
  but it is silent.

**Two machines appending to one sid.** Once the join travels, both machines
can resume the same sid and append different turns. With whole-file sync, the
two `<sid>.jsonl` files and the two envelopes diverge. That is a hard conflict:
the files cannot be merged (fact 1), and picking one side discards turns the
other side's transcript still shows. Keeping both means forking one of them to
a new sid, which rewrites `session_id`. That field is safe to rewrite, since
upstream rewrites it on every import. But the fork then needs a new session key
and its own transcript. Meanwhile `sessions/<key>.jsonl` has already
union-merged both machines' turns into one file. So the two halves disagree in
exactly the way the TODO warns about ("what `pack` does when the two halves
disagree"), and nothing upstream would recognise the result.

**Machine-specific envelope fields and ADR 0001.** `cwd` and
`allowed_*_paths` are exactly the kind of value ADR 0001 translates, and
translating them does not touch signed content (fact 2). So technically ADR
0001 *could* apply: encode on unpack, decode on pack, with the rule scoped to
those three keys. But that means parsing and re-writing the envelope, which
gives up "opaque whole file". And it would **translate** the paths where
upstream deliberately **clears** them, handing the receiving machine write
permissions for a checkout that upstream decided should arrive unscoped.
`session_id` has no portable form at all. The map's own `cwd` field would need
the same treatment inside the sidecar.

**Other costs.** A rewind or a reclaim on one machine removes the pair there.
Because pack never deletes, the other machine's copy comes back on the next
sync, so a reclaim undoes itself. If deletes did propagate instead, reclaiming
on one laptop would silently remove the other laptop's resumable context.
Neither is acceptable without rules of its own. There is no size cap, so a
tool-heavy log up to and past 40 MB is added to git history on every sync. In
team scope, none of this may travel. Option A is personal-scope only, like
`sessions/*.jsonl`.

A is the only option that would make a session *continue* across machines with
full context. But it needs a map writer that the running gateway will not
overwrite, a conflict rule that splits one conversation into two halves that
disagree, and a delete rule. Upstream provides none of these, and building
them here re-implements `session_transfer.py` from outside KiroCrew, where it
cannot follow upstream's changes.

### Option B: sync backends as a mailbox for `.kcsession.json.gz` bundles ◐ Accepted as an explicit verb, rejected as sync

The sender exports with `GET /api/chat/slots/{slot}/export?include_layer_b=true`
and puts the file on the backend. The receiver installs it with
`POST /api/chat/slots/import`. That route runs upstream's validation, redaction,
size caps, envelope rewrite, file write off the loop, and join on the live map
(`session_transfer.py:_validate_bundle`, `_rewrite_layer_b_envelope`,
`_write_layer_b_files`, `_join_layer_b`). This satisfies every upstream
constraint above, because upstream enforces them.

As a **sync** mechanism it fails:

- **Every import is a new session** (fact 7). If B sends a session on each
  sync, every sync creates another `⇄ <title>` tab on the receiver. Continuing
  the session on the sender and sending it again creates another copy, not an
  update. Upstream has no replace verb and no import key. Deduplication would
  have to live here, as a receiver-side ledger keyed on `(sender machine,
  source slot key, content hash)`. It could suppress re-sends of the same
  content, but it cannot turn a later version into an update. The only way to
  collapse copies is to delete the older import, which is a deleting sync
  tool, and ADR 0002 §4 exists to avoid that.
- **Layer A syncs the copies back.** The import writes a new
  `sessions/<new key>.jsonl`. That is in `ALLOW`, so the next `sync` carries
  the `⇄` copy back to the sender. The original transcript was already on the
  receiver through Layer A. Each machine ends up with the original *and* the
  copy. Only the copy on the receiver can resume through `session/load`.
- **It needs a running gateway and an owner credential.** Import needs the
  live `SessionManager`. With `sessions is None`, `_join_layer_b` returns
  False and the copy is transcript-only. The gateway binds loopback only
  (`dashboard/urls.py:is_local_only`). The export only includes Layer B when
  all three hold: `dashboard.export_include_layer_b` is `true`,
  `?include_layer_b=true` is sent, and the request is an owner dashboard
  request (`session_export.py:_export_layer_b_permitted`,
  `_export_layer_b_requested`, `source_providers.py:is_owner_dashboard_request`).
  The third condition requires `request["app"] == ""`, so **the Crew Sync app's
  own token cannot export Layer B**. An app token can only export slots the
  app owns. An import made with an app token is attributed to the app and is
  never filed (`arrival_folders.py:arrival_folder_id`). A mailbox therefore
  needs the owner's token on the command line. That is the opposite of how the
  sync pipeline runs, which prefers KiroCrew idle or stopped.
- **Coverage is partial.** Export resolves the slot from the gateway's live
  slot table (`state._slots`), so a session that is not loaded answers 404. A
  mid-turn source is exported without Layer B, marked `layer_b_skipped`.
  Incognito and temporary sessions are refused. Each import takes a live slot
  under `MAX_LIVE_SLOTS` (500) and answers 429 at the cap, so bulk installs
  are throttled.

As an **explicit, one-off verb**, such as `send-session <slot>` on the sender
and `inbox` on the receiver, these costs match what the user asked for. A
deliberate "continue this on my other machine" *wants* a fork, is sent once,
has the owner at the keyboard with a running gateway, and fits upstream's own
reason for the file route: two machines that are never online together
(`session_export.py` module docstring, `instances.md` §14.7). **Personal scope
only.** The bundle carries the transcript, and with Layer B the unredacted
context window. Team scope withholds transcripts (ADR 0002 §6). Upstream
applies the same restriction to Layer B in files that "can be shared with
another person". So the review's suggestion of sending sessions to a colleague
over a team backend is not adopted.

### Option C: do nothing ✅ Chosen for `sync`

Layer A keeps syncing as it does today. A session opened on another machine
resumes from the transcript replay (80,000-char budget, scaled down for small
windows), not through `session/load`.

- Nothing can corrupt a signature, because no one touches Layer B.
- Nothing writes the map. It stays `DENY`'d and pruned per machine, which is
  now correctly local (fact 4).
- No running-gateway dependency and no credential, so the running guard's
  idle allowance stays sound for everything `sync` writes.
- Team scope is unaffected: no transcript half travels there.
- The loss is resume fidelity: tool and compaction state, and anything older
  than the replay budget. When a session is continued on both machines, the
  union-merged transcript concatenates the turns. That is an existing Layer A
  property, and this decision neither causes nor fixes it.

## Decision

1. **`sync` does not carry the kiro-cli half.** No second sync root.
   `session_map.json` stays in `DENY`. `~/.kiro/sessions/cli` is never read or
   written by the engine. (Option C)
2. **Moving a resumable session is a separate, explicit verb** that uses
   upstream's bundle and import route over the existing backend
   (`send-session` / `inbox`, option B). It is personal scope only, owner
   credential only, never runs as part of `sync` or the daemon, and keeps a
   receiver-side ledger so the same content is installed at most once. The
   prototype running in parallel decides whether the owner-token and
   live-gateway requirements are acceptable. This ADR does not assume they
   are.
3. **Option A is rejected**, not deferred, while any of the upstream facts it
   conflicts with (3, 4, 6, 7) still hold.

## Consequences

**Positive**

- The engine never handles a byte-exact, signed, unredacted artifact. The
  41% failure mode cannot come from this repo.
- The map-pruning feedback loop fixed on 2026-09-22 cannot come back through
  a sidecar.
- If a resumable copy is ever made, upstream's code makes it, so envelope
  rewrites, caps, redaction of Layer A and filing follow upstream releases.

**Negative**

- A session continued on another machine through `sync` loses its kiro-cli
  context, every time. Users who want full fidelity must use the explicit
  verb, and they get a fork.
- The explicit verb adds a dependency on a running gateway and the owner's
  token, and it shares Layer A's echo: the imported copy's transcript syncs
  back to the sender.
- The TODO's "second sync root" line of work is closed. Anyone reopening it
  must address the join writer, the fork rule and the delete rule above.

## Upstream docs that are behind the code

These were found while checking the facts above. Code wins in every case:

- The docstrings still call the map "an unlocked dict". `session_map.py` now
  has `_MAP_LOCK` (a process-level `RLock`). The remaining hazard is a
  detached or out-of-process writer, not threads.
- "Replaying … a lossy ~8K prefix" (`session_transfer.py`, `instances.md`
  §14.1a). The prefix is now built by `context.py:build_session_replay`
  within `_REPLAY_BUDGET_CHARS` (80,000 chars, scaled 0.2–1.0 by model
  window).
- The `_write_layer_b_files` docstring says it "re-redacts the events". The
  body does not, and the inline comments explain why it must not.
- `instances.md` §14.7 lists two conditions for Layer B in an export. The code
  requires a third, `is_owner_dashboard_request`.

## Revisit if

- Upstream makes import idempotent or replaceable, for example with a stable
  source-session id in the bundle and a replace-on-import mode. Option B could
  then run inside `sync` without piling up copies.
- Upstream adds an offline or authenticated route that only writes the join,
  or kiro-cli's store moves under the data home with a join KiroCrew rebuilds
  itself. Either would remove Option A's worst problem.
- The Crew Sync app can get a token that exports Layer B. The verb could then
  run from the dashboard instead of needing the owner's CLI credential.
- Users routinely continue the same session on several machines and report
  the replay loss as the main problem. That would justify the cost of A's
  fork rule.
- `check_kirocrew_running` starts requiring KiroCrew to be fully stopped
  before a pack. That would weaken fact 3's objection to A, though not the
  others.
