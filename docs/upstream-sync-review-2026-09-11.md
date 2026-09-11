# Upstream sync review — KiroCrew v0.6.0 (released)

- **Date:** 2026-09-11
- **Reviewed against:** `kirodotdev/KiroCrew` tag `v0.6.0` (released 2026-09-05
  per `CHANGELOG.md`), read from the release notes and the changelog at that
  tag — not `main`.
- **Supersedes:** the staging-code caveat in
  [upstream-sync-review-2026-08-29.md](upstream-sync-review-2026-08-29.md),
  which explicitly declined to judge 0.6.0 because "`main` is unreleased
  staging code whose behaviour no user has." That caveat no longer applies —
  0.6.0 is now what every user runs. This review checks whether the previous
  verdict survives contact with the shipped feature.
- **Question:** does 0.6.0's headline multi-instance feature — "Remote
  Crews" — give upstream a first-party answer to cross-device sync, or
  change the case for building it here?
- **Answer:** No. Verdict unchanged: **keep building here.** Remote Crews is
  federation (connect live, run one turn on one remote brain), not sync
  (converge state across independent brains that can be offline). The
  previous review predicted this from `main`-staging code and one issue
  comment; this review confirms it against the shipped release and the
  issue tracker's current state.

## What shipped

From `CHANGELOG.md` at `v0.6.0` (2026-09-05), the relevant feature is listed
as **"Remote crews become one dashboard (Preview)"** — still explicitly a
preview, gated behind two switches:

- `instances.enabled` in `config.json`
- Settings → Developer → Feature Previews → "Chat on a crew"

What it does, in the changelog's own words:

> "Run a chat on another crew you are connected to: set `instances.enabled`
> in `config.json`"

> "A session that runs elsewhere carries a server badge and the crew's name
> in your sidebar"

> "Crews connect themselves on app load and on tab focus, on by default and
> switchable at Settings → Remote Instances"

> "The transcript stays local while every turn runs on that crew"

The last line is the load-bearing one: **transcript local, execution
remote.** Nothing here exports, imports, or reconciles a database. It is a
routing/execution decision made per chat, not a data-convergence mechanism.
SSH/SSM appear elsewhere in the release as transport for *registering* a
remote instance (i.e. establishing the connection), not as a sync
transport.

## Mechanism check: is this "session splitting between web and cloud"?

No, not in the sense that matters for this project. Two different problems
share the word "multiple servers":

| | Remote Crews (0.6.0) | `kirocrew-sync` (this project) |
|---|---|---|
| Model | client connects to *one* remote crew and runs a turn there | every machine is a fully independent brain |
| Requires live connectivity | yes — SSH/SSM to the remote instance | no — asynchronous, offline-first |
| What moves | nothing is merged; execution happens elsewhere, transcript stays local | rows: knowledge, memory, sessions, three-way merged |
| Two machines edited independently | not a scenario this addresses — there is one brain per turn | the core case it is built for |
| State after use | the *other* crew's knowledge/memory did not change; only that one chat ran there | every synced machine converges to the same rows |

Running chats on crew A sometimes and crew B other times, with Remote Crews
alone, does **not** keep A's and B's knowledge/memory in sync — it just
chooses, per chat, which brain executes. That divergence is exactly what
`kirocrew-sync`'s three-way merge exists to close. The two are
complementary layers (live routing vs. background convergence), not
competitors.

## Issue tracker, rechecked against the shipped release

The previous review cited four open, unassigned upstream issues as evidence
that cross-device sync was a real gap. Rechecked now that 0.6.0 shipped:

| Issue | Title | State (2026-08-29) | State now (2026-09-11) | What changed |
|---|---|---|---|---|
| #6223 | Multi-machine memory sync / shared workspace across devices | Open, unassigned, `needs-human` | **Closed, not planned**, labeled `duplicate` | Upstream declined to build it rather than folding it into Remote Crews. The closing comment/duplicate target did not render through the fetch used here — worth a manual look if the distinction matters later — but the labels and state are unambiguous: this specific ask is closed, not merged into another effort. |
| #4923 | Add first-class Kiro Cloud Session support | Open, unassigned, `needs-human` | **Still open** | Asks for resuming a hosted "Kiro Cloud" session across devices — a different thing from Remote Crews, which connects to peer *self-hosted* crews you already run. 0.6.0 did not touch this. |
| #3278 | Hybrid local/remote execution: offload to a persistent remote instance | Open | **Still open** | Remote Crews covers a slice of this ask (manual, per-chat offload) but not the "centralized memory, single source of truth" or "intelligent automatic routing" the issue asks for. Upstream has not closed it against 0.6.0, so upstream itself does not consider this delivered. |
| #6340 | Isolated memory containers: scope memory to a workspace or group of projects | Open, unassigned | **Still open** | Unrelated to 0.6.0; unchanged. |

The one status change (#6223 closed as not planned) is, if anything,
stronger evidence for the verdict than the previous review had: upstream
was asked directly for multi-machine memory sync and explicitly declined,
rather than pointing at Remote Crews as the answer.

## Verdict

Unchanged from 2026-08-29, now confirmed against shipped code rather than
staging: **keep building here.** Upstream's 0.6.0 direction is federation
(one brain, reachable from more places, live), which this review already
expected from the issue-tracker language ("Remote Crew lets the MacBook
connect as a client, but the mental model is 'one brain, one machine'") —
that quote is now attached to a *closed, not-planned* issue, not an open
one someone might still resolve upstream's way.

Nothing in this release invalidates ADR 0002 or the recommendation in the
previous review. The two follow-ups named there remain the right next
steps:

- `export` / `import --mode replace` as the seeding primitive.
- Revisit contributing the design to a first-party home if upstream ever
  reopens the question — there currently isn't one to contribute to; #6223
  is closed and #3278/#4923 want different things (execution offload and
  hosted-cloud session parity, not row-level convergence).

## What would change this verdict

Named explicitly so a future review can check it quickly, rather than
re-deriving it:

- Remote Crews graduating out of Preview with a mode where the *knowledge
  base itself* (not just chat execution) is shared or merged across
  connected crews — i.e. #6223 or #3278 reopened and actually built.
- A first-party `export`/`import` or snapshot mechanism gaining real
  three-way merge (not `INSERT OR IGNORE` union, not whole-table replace).
- `kirodotdev/KiroCrew` shipping row-level conflict resolution for
  `knowledge.db`, which `snapshot.py` still replaces wholesale as of
  0.5.0-insider.3 (unverified against 0.6.0's `snapshot.py` — worth a check
  if a future review has reason to look at snapshot/restore again).

## Verification

This is a research-only review: no code, tests, or ADRs changed as a
result. Claims above are sourced from `CHANGELOG.md` at the `v0.6.0` tag
and the live state of issues #6223, #4923, #3278, and #6340 on
`kirodotdev/KiroCrew`, fetched 2026-09-11. Where a claim could not be
verified through the fetch used (the exact closing comment on #6223), that
gap is called out above rather than assumed.
