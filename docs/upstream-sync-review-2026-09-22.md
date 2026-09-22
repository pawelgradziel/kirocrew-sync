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
| Export can carry Layer B | #11297 | 2026-09-18 | `dashboard.export_include_layer_b` (off by default) puts the kiro-cli context window into the file, so an import resumes via `session/load` instead of replaying an ~8K prefix. |
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
  redaction pass broke a signature in 41% of 704 real sessions. So the CLI
  `.jsonl` must never go through `merge=union` or any line merge. Two machines
  appending to the same sid is a conflict to keep as two copies, not something
  to merge.
- **Machine-specific fields.** `_rewrite_layer_b_envelope` lists them:
  `session_id`, `cwd`, and
  `session_state.permissions.filesystem.allowed_{read,write}_paths`. `cwd` and
  the path lists are exactly what ADR 0001 path portability could translate.
- **The join is on-loop.** Upstream writes the two files off-loop and does the
  map join on the gateway's event loop. The map is an unlocked dict saved as a
  whole file, so any other writer loses entries. An offline sync writer is safe
  only while KiroCrew is stopped, which the existing running-guard already
  requires.

### Reuse option: sync as a mailbox for session bundles

**Update: built** as `send-session` / `inbox`. See the README section "Sending
one session to another machine" and `tests/test_session_mailbox.sh`. Three
corrections to the note below, found while building it:

- The file export blanks `origin`, so a file import lands directly under
  `Imported`. The "from \<sender\>" filing only happens because `send-session`
  writes the sender's mailbox address into `origin`, a top-level unsigned
  field.
- Import creates a new slot key and sid, and the bundle carries no stable
  source-session id. Dedup is therefore a SHA-256 of the file in a local,
  unsynced ledger.
- The installed copy's transcript (`sessions/<new key>.jsonl`) is in `ALLOW`.
  Personal sync carries it back, so the sender ends up with the original and
  the copy. The mailbox is most useful between machines that do not share a
  personal backend, or to resume with Layer B.

The original note:

Upstream says the file route exists because a tunnel needs both machines up at
once (§14.7). kirocrew-sync backends (S3/R2, rsync) already provide the
asynchronous mailbox that route is missing. A thin `send-session`/`inbox`
command could put `.kcsession.json.gz` files on the backend and install them on
the other side with `POST /api/chat/slots/import`. That gets upstream's
validation, redaction, size caps, and `Imported / from <sender>` filing for
free, and would work for sending a session to a colleague over a team backend.
This is the supported way to move a *resumable* session, as opposed to the
raw-file approach in the TODO. Worth considering before building a second sync
root.

### Found, not fixed: member memory stores do not sync

v0.7.0 added isolated per-member memory (#9553, simplified to one SQLite store
per member in `bc0d2b0cf`) under `<data home>/memory_stores/`. That path is not
in `ALLOW`, and `DATABASES` in `policy.py` only knows `memory.db` and
`knowledge.db`. Crew-member lessons and memories stay on the machine that
learned them. Closing this needs a row-level policy for the new store schema,
not an allowlist line, because `**/*.db` is denied on purpose.

## Recommendation

Upstream is not building session sync or shared sessions, so the verdict from
earlier reviews stands: keep building here. For the session half specifically,
prefer upstream's bundle format and import route over copying CLI files by hand.
It is versioned (`bundle_version` 2, with additive keys) and already handles the
signature and path problems above.
