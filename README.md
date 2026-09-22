# KiroCrew Sync

**A KiroCrew app** — installs into KiroCrew and adds a **Crew Sync** page to
the sidebar — plus the cross-platform sync engine behind it, with modular
storage backends. Usable either way: run the CLI directly, or drive it from
the dashboard.

**Work on two machines without choosing which one wins.** Most sync tools for
this kind of data copy a directory in one direction and overwrite whatever was
on the other side. KiroCrew Sync does a real **three-way merge** instead: it
remembers the state at your last sync, works out what changed on each machine
since then, and combines both.

```bash
./kirocrew-sync.sh sync
```

## As a KiroCrew app

```bash
./install-app.sh
```

Then trust it under **Settings → Security → Third-party apps** (KiroCrew denies
third-party app code by default) and enable it under **Apps → Crew Sync**. The
dashboard page gives you sync status, a history timeline, conflict and
quarantine panels, daemon controls and backend configuration with a
"Test Connection" button — everything the CLI does, without the CLI.

See [`app/README.md`](app/README.md) for the app's architecture, its API
routes and how to build the UI bundle, and [`GOAL.md`](GOAL.md) for the
current state and its known limitations.

## What is it for?

**Primarily: keeping one person's KiroCrew data consistent across their own
computers.** Laptop, desktop, work machine — you sit down at whichever one is
in front of you, run `sync`, and your chat history, knowledge base, artifacts
and learned lessons are there, merged rather than overwritten. That is the case
it is designed and tested for. It is not limited to two machines: each machine
publishes its own state and merges every other machine's.

**A team can also share one knowledge library**, using `--team`:

```bash
./kirocrew-sync.sh sync --team
```

Team scope publishes the knowledge base, artifacts, tags and learned lessons,
and holds back everything personal — chat transcripts, episodic memory, and
per-person config. It is off by default, so nothing changes unless you ask for
it. See [Team scope](#team-scope) for exactly what travels and what does not.

Two caveats that apply to team use regardless of scope:

- **No access control.** Anyone who can read the storage backend gets
  everything that was published to it. Scope decides what is published; it does
  not decide who may read it.
- **Concurrent publishing races.** A machine that publishes between another
  machine's pull and push has its bundle removed from the remote. Nothing is
  lost — it is restored on that machine's next sync — but the window is hit
  more often the more people are syncing.

If you need per-user permissions or many simultaneous writers, this is the
wrong tool.

## Working from any device

[kirocrew-at-cloudflare](https://github.com/pawelgradziel/kirocrew-at-cloudflare)
runs one KiroCrew brain in a Cloudflare Container, reachable from any browser
(laptop, Mac, or phone) behind Cloudflare Access. This engine is how that
cloud crew and your local ones converge: the container is an opt-in peer in
the same personal/team sync mesh described above, publishing to and pulling
from an S3-compatible bucket (R2) exactly like a laptop would - no special
case in the engine for "cloud." See
[docs/working-from-anywhere.md](https://github.com/pawelgradziel/kirocrew-at-cloudflare/blob/main/docs/working-from-anywhere.md)
in that repo for the full setup, from zero to every device.

## Why three-way sync matters here

A one-directional copy forces you to remember which laptop has newer data, and
punishes you when both do. Three-way sync removes that problem:

- **Both sides' work is kept.** A lesson learned on your laptop and a knowledge
  item added on your desktop both survive. They are different rows, so they do
  not conflict at all.
- **Conflicts are per row, not per file.** Two machines editing different
  entries in the same database is a normal merge, not a conflict. Only the same
  row edited on both sides needs a decision.
- **No clock-skew guesswork.** The merge base is found by content hash, not by
  comparing timestamps across machines whose clocks may disagree.
- **Deletions never silently beat edits.** If one machine deletes something the
  other machine edited, the edit survives by default.
- **More than two machines work.** Each machine publishes its own state; every
  other machine merges it.

This is possible because sync operates on an *unpacked* form of your data —
databases are exported to one text file per table, merged row by row, then
written back. That design also fixes a subtler problem: KiroCrew's SQLite
databases run in WAL mode, and their `.db` files are frequently near-empty
while the real content sits in the `-wal`. Copying the files can move a 4 KB
stub. Exporting through SQLite cannot.

See [ADR 0002](docs/adr/0002-three-way-sync-via-unpacked-git-repo.md) for the
full design and its trade-offs.

## Features

- 🔀 **Three-way merge**: real bidirectional sync, per row, with conflict strategies
- 🔌 **Modular backends**: Google Drive, S3, rsync, local directory, or custom
- 🖥️ **Cross-platform**: Works on Linux and macOS
- 🧭 **Portable paths**: Knowledge folder sources keep working on machines with a different layout
- 🔐 **Credential-safe**: explicit allowlist; API tokens never leave the machine
- ↩️ **Reversible**: every sync is a git commit; databases are backed up before writes
- 🔎 **Inspectable**: `doctor`, `status`, `paths` and `--dry-run` show exactly what will happen

## What Gets Synced

**Databases** (merged row by row):

| Source | Contents |
| --- | --- |
| `memory.db` | Semantic and episodic memory, learned lessons, memory event log |
| `workspace/knowledge/knowledge.db` | Knowledge items, entities, relations, sources |
| `memory_stores/<name>/memory.db` | Each crew member's private memory (KiroCrew 0.7+): facts, lessons, episodes, daily history, revision journal, event log, store identity. Personal scope only |

Member memory stores are found by scanning `memory_stores/` on every sync.
Each store is its own database in the sync repo (`db/memory_stores/<name>/`),
with its own schema and embedding checks. A store that exists on only one
machine is created on the other from its recorded schema. Store names are
validated the way KiroCrew validates them, and a store directory that is a
link to somewhere else is skipped.

**Files** (merged structurally or by union):

| Source | Contents |
| --- | --- |
| `sessions/*.jsonl`, `sessions/archive/*.jsonl` | Chat transcripts, live and archived (append-only union) |
| `config.json`, `tags.json`, `tag_boards.json` | Settings (merged key by key) |
| `connections_ui_migrated.json`, `superseded_acked.json` | `config.json`'s one-shot migration ledgers, so no machine re-runs a migration over a value another kept on purpose |
| `hooks.json`, `model_windows.json` | Runtime metadata |
| `workspace/memory/` | Workspace memory notes |
| `artifacts/` | Saved widgets and artifacts |
| `memory_stores/<name>/memory/*.md`, `memory/history/*.md` | A member's preferences and projects; a named store's daily history |
| `memory_stores/<name>/lessons.jsonl`, `member-memory.json` | A named store's lesson file; the legacy ownership manifest |

**Deliberately not synced:**

- `memory_index.db` and FTS indexes — derived data, rebuilt locally after every sync (this includes each store's `memory_fts` and a named store's own `memory_index.db`)
- Host-local state under `memory_stores/`: `.member-api-key`, `.member-backups/` (rolling backups, restore journals, locks), `.execution-logs/`, and a named store's `backups/` — the same set KiroCrew's own export leaves out
- `mcp.json`, `.local_secret`, `token_signing.key`, `sel_hmac.key` — credentials
- `security_events.jsonl`, `audit.log`, `gateway.log` — local audit logs (often 30 MB+)
- `.machine_id`, `path_map.conf`, PID files, lock files, `run/`, `cache/`, `logs/`
- Machine-local database tables: filesystem scan state and transient job state
- `crons.json` — a synced copy would make every machine fire the same scheduled jobs (duplicate executions, duplicate notifications)
- `notifications.jsonl` — per-machine delivery history; a notification fired on one machine is not a fact about another
- `session_map.json` — points each session at a kiro-cli context in `~/.kiro/sessions/cli`, which does not sync; KiroCrew prunes entries whose context is missing, and a synced copy spread that pruning back to the machine that owned them
- `autonudge.json` — live self-prompting loops; like `crons.json`, a synced copy would make every machine fire them, and KiroCrew rewrites the store from host state at startup

Credential-shaped fields inside synced JSON (`bot_token`, `app_password`,
`api_key`, …) are stripped before upload and restored from your local file
afterwards, so each machine keeps its own.

The same happens to a few `config.json` fields that describe the machine rather
than your settings: `memory.embed_model_stamp` (a `stat()` of the local custom
embedding model) and `memory.embed_model_legacy_ids`.

## Installation

### Prerequisites

The sync engine needs **Python 3.8+** (standard library only, including
`sqlite3`) and **git**, which supplies the merge base and conflict handling.
Both ship with macOS and most Linux distributions.

The default backend is Google Drive, which needs [rclone](https://rclone.org/):

```bash
# macOS
brew install rclone

# Linux
curl https://rclone.org/install.sh | sudo bash
```

Other backends need their own tools — see [Storage Backends](#storage-backends).

### Setup

1. **Clone this repository:**
   ```bash
   git clone https://github.com/pawelgradziel/kirocrew-sync.git
   cd kirocrew-sync
   ```

2. **R2 / S3-compatible mesh (one command):**
   ```bash
   ./onboard.sh
   ```
   Discovers your Cloudflare account over wrangler's OAuth session, writes
   `config.sh`, walks the one dashboard-only step (minting the R2 key pair —
   no OAuth path exists for that), verifies the bucket with the exact probe
   the backend runs, and offers the first sync. Safe to re-run; `--scope
   team`/`both` also writes the team-scope config. Everything below remains
   the manual equivalent.

3. **Manual path — initialize configuration:**
   ```bash
   ./kirocrew-sync.sh init
   ```

4. **Configure your storage backend** — Google Drive is set up below; for the
   others follow the linked pages in [Storage Backends](#storage-backends)

## Storage Backends

| Backend | `SYNC_BACKEND` | Needs | Setup |
| --- | --- | --- | --- |
| **Google Drive** (default, recommended) | `gdrive` | `rclone` | [below](#google-drive-default) |
| AWS S3, or any S3-compatible store (Cloudflare R2, MinIO) | `s3` | `awscli` | [docs/backends/s3.md](docs/backends/s3.md) |
| Rsync (direct host or NAS) | `rsync` | `rsync`, SSH access | [docs/backends/rsync.md](docs/backends/rsync.md) |
| Local directory (Dropbox, Syncthing, NAS mount, USB) | `local` | nothing | set `LOCAL_SYNC_DIR` |
| Your own | any | whatever you script | [docs/backends/custom.md](docs/backends/custom.md) |

### Google Drive (Default)

Uses [rclone](https://rclone.org/) to sync with Google Drive. This is what
`SYNC_BACKEND` defaults to, so nothing else needs configuring once the steps
below are done.

#### Setup Instructions

1. **Configure rclone for Google Drive:**
   ```bash
   rclone config
   ```

2. **Follow the prompts:**
   - Choose: `n` (New remote)
   - Name: `kirocrew-gdrive` (or edit `backends/gdrive.sh` to use different name)
   - Storage type: `drive` (Google Drive)
   - Scope: `drive` (Full access to all files)

3. **OAuth Credentials (two options):**

   **Option A: Use rclone's built-in credentials** (easiest)
   - Leave `client_id` and `client_secret` blank
   - Follow the browser authorization flow

   **Option B: Create your own OAuth app** (recommended for privacy)
   - Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
   - Create a new project (or select existing)
   - Enable the Google Drive API:
     - Visit [Google Drive API Library](https://console.cloud.google.com/apis/library/drive.googleapis.com)
     - Click "Enable"
   - Create credentials:
     - Click "Create Credentials" → "OAuth client ID"
     - Application type: "Desktop app"
     - Name: "KiroCrew Sync" (or any name you prefer)
     - Copy the Client ID and Client Secret
   - Enter them in rclone when prompted
   - Complete the browser authorization flow

4. **Test the connection:**
   ```bash
   rclone lsd kirocrew-gdrive:
   ```

5. **Start syncing:**
   ```bash
   ./kirocrew-sync.sh push   # Push from this machine
   ./kirocrew-sync.sh pull   # Pull from another machine
   ```

**Security Note:** Your OAuth tokens are stored in `~/.config/rclone/rclone.conf` and are automatically excluded from git commits.

### Other Backends

Prefer somewhere else? Set up [AWS S3](docs/backends/s3.md) — or Cloudflare R2
or any other S3-compatible store, through that same backend and the same `aws`
CLI — sync straight to a machine you own with [rsync](docs/backends/rsync.md),
point the `local` backend at a folder that already reaches your other machines,
or
[write your own](docs/backends/custom.md) — a backend is one bash file with
three functions.

```bash
# Local directory: a Dropbox/Syncthing folder, a NAS mount, a USB drive
export SYNC_BACKEND=local
export LOCAL_SYNC_DIR="$HOME/Dropbox/KiroCrew-Sync"
```

## Usage

### Basic Commands

```bash
# Two-way sync: merge local and remote changes
./kirocrew-sync.sh sync

# See what would happen, without writing or publishing anything
./kirocrew-sync.sh sync --dry-run

# Inspect local data and run preflight checks
./kirocrew-sync.sh doctor

# Show pending changes and backend state
./kirocrew-sync.sh status

# Check that knowledge source paths survive a sync
./kirocrew-sync.sh paths

# Show help
./kirocrew-sync.sh help
```

### Conflict strategies

A conflict means *the same row was edited on both machines since the last sync*.
Different rows, different tables, and different files are merged without asking.

```bash
# Default: keep the most recently updated version of each row
./kirocrew-sync.sh sync --strategy auto

# Prefer this machine on every conflicting row
./kirocrew-sync.sh sync --strategy local-wins

# Prefer the other machine
./kirocrew-sync.sh sync --strategy remote-wins

# Stop and let you resolve conflicts by hand
./kirocrew-sync.sh sync --strategy manual
```

With `manual`, resolve the conflicts inside `~/.kiro/crew/.sync/repo`, then run
`./kirocrew-sync.sh resume`. Automatic resolutions are always reported and
logged to `~/.kiro/crew/.sync/conflicts.jsonl`.

### One-directional commands

`sync` is the command you want. These remain for the cases where you know which
side should win:

```bash
# Publish local state without merging
./kirocrew-sync.sh push

# Sync, preferring remote on conflict
./kirocrew-sync.sh pull
```

### Seeding a new machine

`export` packages this scope's synced state into one file; `import` seeds a
machine's local state from one. Use this to bootstrap a new machine straight
from a laptop that already has good data, without both machines needing to
reach the same backend at once:

```bash
# On the machine with good data
./kirocrew-sync.sh export -o snap.tar.gz

# Copy snap.tar.gz to the new machine by any means, then:
./kirocrew-sync.sh import snap.tar.gz
```

The archive carries real git history, not just a snapshot of the data — the
seeded machine ends up with an actual merge base, so its first ordinary
`sync` with the rest of the pair needs no real merging at all, rather than
merely merging cleanly. `import` refuses if this machine already has local
state, and says exactly what `--force` would replace:

```bash
./kirocrew-sync.sh import snap.tar.gz --force
```

There is no merge mode. A merge needs two live states *and* their common
ancestor; a standalone archive has no ancestor against this machine's own
data, so there is nothing for "merge" to mean here — that is what `sync`
does, once both machines share real history. `import` only ever replaces,
and only touches an existing machine's state at all when told `--force`.

Both commands respect `--team`/`--scope`, the same as `sync`: an archive
exported at one scope refuses to import into the other, so a personal export
can never seed a colleague's team library by accident.

### Sending one session to another machine

`send-session` and `inbox` use the backend as a mailbox for single chat
sessions. They move KiroCrew's own session export file, so the receiving
KiroCrew does the validation, size limits, credential redaction and filing.
Both commands talk to the **running** KiroCrew. They never read or write
KiroCrew's data directories.

```bash
# On the sending machine, with KiroCrew running
./kirocrew-sync.sh send-session slot-3 --to desktop   # or dashboard:slot-3

# On the receiving machine, with KiroCrew running
./kirocrew-sync.sh inbox                 # list what is waiting (no gateway needed)
./kirocrew-sync.sh inbox --install       # install everything new
./kirocrew-sync.sh inbox --install <name>
./kirocrew-sync.sh inbox --discard <name>
```

- **Addresses.** A machine's address is `MAILBOX_NAME` from `config.sh`, or
  its machine id if that is not set. `--to all`, the default in personal
  scope, reaches every machine on the backend except the sender. Each
  address is a folder under `<backend root>/mailbox/`, next to `bundles/`.
- **What moves.** `send-session` calls `GET /api/chat/slots/{slot}/export` and
  uploads the gzipped bundle. The slot has to be loaded in KiroCrew (open as a
  tab), or the export returns 404. Incognito and temporary sessions cannot be
  exported. `inbox --install` posts the file unchanged to
  `POST /api/chat/slots/import`, which creates a new session under
  **Imported / from \<sender\>**. The sender's name is there because
  `send-session` writes its mailbox address into the bundle's `origin` field.
  KiroCrew's own file export leaves that field empty.
- **Installed once.** Import never overwrites. Every import creates a new
  session, so installing a bundle twice gives you a duplicate. `inbox` keeps a
  local record in `~/.kiro/crew/.sync/mailbox-state.json`, which is never
  synced. The record is keyed by a SHA-256 of the file, so the same bytes
  arriving under another name are skipped as well. A bundle sent to this
  machine is deleted from the backend once it is installed or discarded. A
  bundle sent to `all` stays on the backend for your other machines.
  `inbox --install <name> --force` installs a bundle again anyway.
- **It never enters sync.** Every backend's push and pull exclude `mailbox/`.
  Mailbox files never reach the sync repo, and a sync's mirror-with-delete
  never removes them.
- **Personal sync brings the copy back.** An installed session is an ordinary
  session. Its transcript is `sessions/<new key>.jsonl`, which personal sync
  carries to every machine, including the sender, which then has both the
  original and the copy. Between machines that already share a personal sync
  backend, `sync` alone already makes the transcript readable everywhere.
  `send-session` is most useful where that is not the case: a machine on a
  different backend, a colleague, or a session you want to **resume** on the
  other side, which needs Layer B (below).
- **Layer B.** `--include-layer-b` asks KiroCrew to include the session's
  kiro-cli context, so the copy resumes through `session/load` instead of
  replaying a history prefix. KiroCrew only does this when
  `dashboard.export_include_layer_b` is `true` in its `config.json`. Otherwise
  it withholds Layer B and `send-session` says so. Layer B is byte-exact and
  **unredacted**, because its thinking-block signatures break if any byte
  changes.
- **Team scope is explicit.** Team sync never publishes transcripts, so with
  `--team` (or `SYNC_SCOPE=team`), `send-session` requires one named
  recipient (`--to <name>`, not `all`) and `--share-transcript` to confirm.
  The bundle holds the full visible conversation. It sits unencrypted on the
  team backend, readable by anyone with access, until the colleague installs
  or discards it.

**Authentication.** Both commands authenticate the way KiroCrew's own local
tools and `install-app.sh` do. They read the gateway secret from
`~/.kiro/crew/run/gateway-<port>.secret`, falling back to
`~/.kiro/crew/.local_secret`. They mint a 10-minute dashboard token with
`GET /api/token/local` (`X-Local-Secret` header, loopback only), then call the
two routes with `?token=`. That token is an owner token with no app scope, so
it can export any loaded slot, may carry Layer B, and gets the "from
\<sender\>" filing. An app-scoped token would get none of those. The
dashboard's unix socket (`~/.kiro/crew/dashboard-<port>.sock`) is used when it
exists, and loopback TCP otherwise. The port comes from `KIROCREW_PORT`, then
`KIROCREW_BOUND_PORT`, then a port written in `dashboard.url` in KiroCrew's
`config.json`, then the only `run/gateway-<port>.bin` marker, then 5476. If
KiroCrew is not running, or no secret can be read, or the gateway refuses the
token, the command says which one happened and changes nothing.

### Switching Between Backends

```bash
# Use Google Drive
SYNC_BACKEND=gdrive ./kirocrew-sync.sh sync

# Use S3
SYNC_BACKEND=s3 ./kirocrew-sync.sh sync

# Use rsync
SYNC_BACKEND=rsync ./kirocrew-sync.sh sync
```

Or edit `config.sh` to set the backend for every run:
```bash
export SYNC_BACKEND="gdrive"  # or s3, rsync, local
```

The environment wins over `config.sh`, so the one-off form above overrides the
configured backend for a single run. The same order — environment, then
`config.sh`, then the built-in default — applies to `KIROCREW_DIR`,
`SYNC_PORTABLE_PATHS` and `KIROCREW_PATH_MAP`.

## Typical Workflow

### Setting up two laptops

**On Laptop A (first machine):**
```bash
cd kirocrew-sync
./kirocrew-sync.sh init
# Configure your backend (see Storage Backends above)
./kirocrew-sync.sh doctor
./kirocrew-sync.sh sync
```

**On Laptop B (second machine):**
```bash
git clone https://github.com/pawelgradziel/kirocrew-sync.git
cd kirocrew-sync
./kirocrew-sync.sh init
# Configure the SAME backend as Laptop A
./kirocrew-sync.sh sync
```

**Daily usage:**
```bash
# Whichever laptop you sit down at, and again when you finish
./kirocrew-sync.sh sync
```

There is no push/pull ordering to remember. Changes made on both machines are
merged, not overwritten.

### Automated Sync

The recommended way is the **background daemon**, which polls intelligently and adapts to activity:

```bash
# Test it first
./kirocrew-sync.sh daemon

# Then install as a service (see docs/daemon.md for full instructions):
# Linux:   systemctl --user enable kirocrew-sync.service
# macOS:   launchctl load ~/Library/LaunchAgents/com.kirocrew.sync.plist
```

The daemon handles both use cases:
- **Personal**: switching between your own machines
- **Team**: new knowledge from colleagues propagates automatically

See **[docs/daemon.md](docs/daemon.md)** for installation and configuration.

**Simpler alternatives** (less adaptive):

**Option 1: Shell profile hook (sync before each KiroCrew launch)**
```bash
# Add to ~/.bashrc or ~/.zshrc
alias kirocrew='~/.kiro/crew/workspace/kirocrew-sync/kirocrew-sync.sh sync && command kirocrew'
```

**Option 2: Cron job (fixed schedule)**
```bash
# Edit crontab
crontab -e

# Sync every hour (skips the run if KiroCrew is open)
0 * * * * cd /path/to/kirocrew-sync && ./kirocrew-sync.sh sync 2>&1 | logger -t kirocrew-sync
```

## Knowledge Paths Across Machines

A knowledge folder source is stored as a plain filesystem path. Added on a
laptop, `/home/alice/dev/groover/docs` means nothing on a desktop where the same
repository lives under `/home/alice/work/projects/groover/docs` — the source
goes missing, and with it the entity graph built from those documents.

This tool translates those paths at the sync boundary:

```
unpack:  /home/alice/dev/groover/docs  ->  ~/dev/groover/docs   (in the synced form)
pack:    ~/dev/groover/docs            ->  /Users/pawel/dev/groover/docs
```

Only the copy in transit is portable. Your local `knowledge.db` always holds
real absolute paths, because KiroCrew resolves source URIs with a bare
`Path(uri)` and does not expand `~` — a tilde stored in the live database would
break every folder source, including on the machine that created it. The
reasoning and the options weighed are recorded in
[ADR 0001](docs/adr/0001-knowledge-path-portability.md).

Two columns travel: the source URI and the tombstones for auto-discovered
project folders. The per-file ingest state stays put — it records this
machine's absolute paths and scan timestamps, so it is machine-local and never
synced at all.

Because paths are translated per row, the portable form is also what the merge
sees: two machines that added the same folder under different local paths
resolve to one row rather than two competing ones.

Run `./kirocrew-sync.sh paths` to see where you stand:

```
ℹ Knowledge source paths on this machine
ℹ Path map: none (/home/pawel/.kiro/crew/path_map.conf)

  3 source(s), 3 path-based
  ✓ Groover Docs [local_folder]
      /home/pawel/code/groover/docs
      syncs as ~/code/groover/docs
  ✗ Old Notes [local_folder]
      /home/pawel/archive/notes
      path not found on this machine
      syncs as ~/archive/notes
  ⚠ Team Wiki [local_folder]
      /mnt/shared/wiki
      outside $HOME and unmapped -- will not survive sync

  portable: 2   will not survive sync: 1   missing on this machine: 1
  → give machine-specific paths a name in the path map so they travel:
      see path_map.conf.example
```

### When `~` is not enough

Anything under `$HOME` travels for free, including across Linux and macOS. Two
cases need help:

- the location sits outside `$HOME` (`/mnt/shared`, `/opt`)
- the same repositories live under different subdirectories of `$HOME` on each
  machine (`~/dev` here, `~/work/projects` there)

For those, name the location once per machine in `~/.kiro/crew/path_map.conf`:

```bash
cp path_map.conf.example ~/.kiro/crew/path_map.conf
```

```bash
# On the laptop
CODE   = ~/dev
SHARED = /mnt/shared

# On the desktop
CODE   = ~/work/projects
SHARED = /Volumes/shared
```

A source at `~/dev/groover/docs` then travels as `${CODE}/groover/docs` and
lands on the desktop as `~/work/projects/groover/docs`. The map is
machine-specific and is never synced — that is what makes it work.

When a `${NAME}` has no entry on the receiving machine, the path is left
visibly unresolved and reported, rather than silently pointing a source at the
wrong directory.

### Notes and limits

- **Relative paths** (`./docs`) are reported and left alone. They resolve
  against the working directory of whatever process added them, which is not
  knowable later — guessing would invent a wrong path.
- **Symlinks are not resolved.** The logical path you typed
  (`~/projects/groover`) travels better than what it points at on one machine
  (`/mnt/storage/repos/groover`).
- **Duplicates are never merged.** If two sources differ only in spelling
  (`/home/you/docs` and `~/docs`), normalizing would collide on a UNIQUE
  constraint; the row is left as-is and reported so you can merge it yourself.
- **Paths outside `$HOME` with no mapping stay absolute.** Nothing here can
  make them portable, so they are flagged rather than mangled.
- Set `SYNC_PORTABLE_PATHS=0` to sync URIs verbatim, and
  `KIROCREW_PATH_MAP=/some/file` to keep the map elsewhere.
- Requires `python3` (standard library only — no packages to install). Without
  it, sync still works and warns that paths are travelling verbatim.

## Team Scope

By default, sync is **personal**: it assumes every machine belongs to you and
moves everything syncable between them. `--team` switches to sharing a library
with colleagues, and narrows what leaves the machine.

```bash
./kirocrew-sync.sh sync --team
```

Set it permanently in `config.sh` instead of typing it each time — forgetting
the flag once is the failure mode this is meant to avoid:

```bash
export SYNC_SCOPE="team"
```

### What travels

| | Personal | Team |
|---|---|---|
| Knowledge base (sources, items, entities, relations, mentions) | ✅ | ✅ |
| Learned lessons (`semantic_memory`) | ✅ | ✅ |
| Artifacts, `tags.json`, `tag_boards.json` | ✅ | ✅ |
| Chat transcripts, live and archived (`sessions/`) | ✅ | ❌ |
| Episodic memory — raw conversation text | ✅ | ❌ |
| Memory event log | ✅ | ❌ |
| Crew member memory stores (`memory_stores/`), lessons included | ✅ | ❌ |
| Personal config (`config.json`, `hooks.json`, …) | ✅ | ❌ |
| Per-machine ingest state (`folder_file_state`) | ✅ | ❌ |
| API tokens and credentials | ❌ | ❌ |

Team scope is an **allowlist**: a table or file travels only if it is
explicitly marked shared. A table added by a future KiroCrew version therefore
stays on the machine until someone decides it is safe to publish — the only
direction in which a wrong guess is harmless.

### Things worth knowing

- **Each scope keeps its own sync repo** (`.sync/repo` and `.sync/repo-team`),
  so one machine can sync personally with one config and with a team using
  another. They have separate merge bases and never see each other's data.
- **Mixing scopes is refused, not merged.** A machine syncing personally
  against a team backend is quarantined with a scope mismatch, so forgetting
  `--team` once cannot publish your transcripts into the team's history.
- **Folder source paths are visible to colleagues.** `sources.uri` has to
  travel — every knowledge item references it — so a folder source added on
  your machine shows up as `~/code/whatever` for the team. Portable-path
  encoding (ADR 0001) strips your home directory, not the rest of the path.
- **Member memory stays personal.** KiroCrew keeps each crew member's memory
  private to that member. A member's lessons are in the same table as their
  raw conversation episodes, so the lessons cannot be shared without the
  episodes. Team scope does not look at `memory_stores/`, and does not publish
  the store names either (a store name contains the member id).
- **Team scope does not retract what personal scope already published.** If you
  synced a backend personally and then switch it to team, the earlier data is
  still in that repo's history. Start a team scope against a fresh backend
  location.

## Safety Features

- **Running check**: refuses to write merged data while KiroCrew is running.
  Reading is safe, so `--dry-run`, `status`, `doctor` and `paths` work any time.
- **Automatic backups**: every database is copied to
  `~/.kiro/crew/.sync/backups/<timestamp>/` before any write, and restored
  automatically if the write fails.
- **Transactional writes**: all changes to a database apply in one transaction,
  with foreign-key ordering and a `PRAGMA foreign_key_check` afterwards.
- **Schema gate**: refuses to merge machines running different KiroCrew
  versions instead of blending incompatible schemas.
- **Embedding gate**: refuses to mix vectors from different embedding models,
  which would otherwise degrade semantic search with no visible error.
- **Quarantine, not deadlock**: a machine either gate rejects is skipped, while
  every other machine still syncs. It is re-checked on each sync and rejoins on
  its own once it catches up, so one lagging laptop never blocks the rest.
- **Credential allowlist**: only explicitly listed paths are ever published.
- **Full history**: every sync is a git commit in `~/.kiro/crew/.sync/repo`,
  so any previous state can be recovered.

## Troubleshooting

### "KiroCrew is running"
Quit the KiroCrew app, then re-run. To look around without stopping it:
```bash
./kirocrew-sync.sh sync --dry-run
```

### "N machine(s) quarantined"
That machine is on a different KiroCrew version or embedding model, so its
changes were skipped. Everything else still synced, and `sync` exits `3` to
say so. Nothing to do here — bring that machine up to date and the next
ordinary sync merges it automatically. `status` lists who is quarantined.

To merge it now anyway, knowing the schemas or vectors differ:
```bash
./kirocrew-sync.sh sync --force
```
`--force` is not scoped to one machine: it drops the compatibility check for
every machine and every preflight gate at once.

### A sync stopped on conflicts
```bash
./kirocrew-sync.sh status                       # see what is unresolved
git -C ~/.kiro/crew/.sync/repo merge --abort    # start over
./kirocrew-sync.sh sync --strategy local-wins   # or remote-wins
```

### Check what will be synced
```bash
./kirocrew-sync.sh doctor
```

### A knowledge source went missing after a sync
```bash
./kirocrew-sync.sh paths
```

Sources marked `✗ path not found on this machine` point at a directory that
does not exist here. Either the layout differs — add a mapping, see
[Knowledge Paths Across Machines](#knowledge-paths-across-machines) — or the
folder really is gone and the source should be removed in KiroCrew.

Sources marked `⚠ outside $HOME and unmapped` still work here but will break on
the next machine. Give them a name in the path map before pushing.

### Google Drive connection problems

```bash
# Re-authenticate
rclone config reconnect kirocrew-gdrive

# Test connection
rclone lsd kirocrew-gdrive:
```

For the other backends, see the troubleshooting section of
[S3](docs/backends/s3.md#troubleshooting) or
[rsync](docs/backends/rsync.md#troubleshooting).

## Tests

```bash
./tests/run_tests.sh                  # two-machine three-way merge, 61 assertions
./tests/test_team_scope.sh            # what team scope shares and withholds
./tests/test_member_stores.sh         # crew member memory stores (memory_stores/)
./tests/test_seed.sh                  # export/import: seeding a new machine
./tests/test_portable_paths.sh        # path translation
./tests/test_sync_paths.sh            # path round trip between two machines
./tests/test_config_precedence.sh     # environment vs config.sh vs defaults
./tests/test_backend_local_list.sh    # the remote-state fingerprint contract
./tests/test_backend_s3_endpoint.sh   # S3 command lines, AWS and S3-compatible
./tests/test_session_mailbox.sh       # send-session / inbox against a fake gateway
./tests/test_daemon.sh                # the polling daemon
```

They simulate a second machine by overriding `$HOME` or by pointing two
throwaway KiroCrew directories at a local-directory backend — and, for the S3
suite, at a stub `aws` earlier on `PATH` that records the command lines it is
given — so they need no remote storage and touch nothing outside a temporary
directory.

## Security Considerations

- **OAuth tokens** (Google Drive): Stored in `~/.config/rclone/rclone.conf` (gitignored)
- **AWS credentials**: Stored in `~/.aws/credentials` (never committed)
- **SSH keys** (rsync): Use your existing SSH setup
- **Data in transit**: All backends use encrypted connections (HTTPS/SSH)
- **Data at rest**: your synced data includes chat history and knowledge, and
  is **not** end-to-end encrypted. Use a storage location you trust.

**Recommendation:** Use your own OAuth credentials for Google Drive rather than rclone's shared ones.

## Contributing

Contributions welcome! Areas for improvement:

- Additional backends (Dropbox, OneDrive, WebDAV) — see
  [docs/backends/custom.md](docs/backends/custom.md)
- End-to-end encryption before upload
- Automatic schema migration instead of refusing on drift

## License

MIT License - see LICENSE file for details

## Author

Created for syncing KiroCrew data across multiple machines.

Repository: https://github.com/pawelgradziel/kirocrew-sync
