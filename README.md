# KiroCrew Sync

Cross-platform synchronization for KiroCrew data with modular storage backends.

**Work on two machines without choosing which one wins.** Most sync tools for
this kind of data copy a directory in one direction and overwrite whatever was
on the other side. KiroCrew Sync does a real **three-way merge** instead: it
remembers the state at your last sync, works out what changed on each machine
since then, and combines both.

```bash
./kirocrew-sync.sh sync
```

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

**Files** (merged structurally or by union):

| Source | Contents |
| --- | --- |
| `sessions/*.jsonl` | Chat transcripts (append-only union) |
| `config.json`, `tags.json`, `tag_boards.json` | Settings (merged key by key) |
| `session_map.json`, `hooks.json`, `model_windows.json` | Session and runtime metadata |
| `workspace/memory/` | Workspace memory notes |
| `artifacts/` | Saved widgets and artifacts |

**Deliberately not synced:**

- `memory_index.db` and FTS indexes — derived data, rebuilt locally after every sync
- `mcp.json`, `.local_secret`, `token_signing.key`, `sel_hmac.key` — credentials
- `security_events.jsonl`, `audit.log`, `gateway.log` — local audit logs (often 30 MB+)
- `.machine_id`, `path_map.conf`, PID files, lock files, `run/`, `cache/`, `logs/`
- Machine-local database tables: filesystem scan state and transient job state

Credential-shaped fields inside synced JSON (`bot_token`, `app_password`,
`api_key`, …) are stripped before upload and restored from your local file
afterwards, so each machine keeps its own.

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

2. **Initialize configuration:**
   ```bash
   ./kirocrew-sync.sh init
   ```

3. **Configure your storage backend** — Google Drive is set up below; for the
   others follow the linked pages in [Storage Backends](#storage-backends)

## Storage Backends

| Backend | `SYNC_BACKEND` | Needs | Setup |
| --- | --- | --- | --- |
| **Google Drive** (default, recommended) | `gdrive` | `rclone` | [below](#google-drive-default) |
| AWS S3 | `s3` | `awscli` | [docs/backends/s3.md](docs/backends/s3.md) |
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

Prefer somewhere else? Set up [AWS S3](docs/backends/s3.md), sync straight to a
machine you own with [rsync](docs/backends/rsync.md), point the `local` backend
at a folder that already reaches your other machines, or
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

You can automate syncing with cron or by adding hooks to your shell profile:

**Option 1: Shell profile hook (automatic)**
```bash
# Add to ~/.bashrc or ~/.zshrc
alias kirocrew='~/.kiro/crew/workspace/kirocrew-sync/kirocrew-sync.sh sync && command kirocrew'
```

**Option 2: Cron job (scheduled)**
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
| Chat transcripts (`sessions/*.jsonl`) | ✅ | ❌ |
| Episodic memory — raw conversation text | ✅ | ❌ |
| Memory event log | ✅ | ❌ |
| Personal config (`config.json`, `hooks.json`, `autonudge.json`, …) | ✅ | ❌ |
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
./tests/run_tests.sh               # two-machine three-way merge, 59 assertions
./tests/test_team_scope.sh         # what team scope shares and withholds
./tests/test_portable_paths.sh     # path translation
./tests/test_sync_paths.sh         # path round trip between two machines
./tests/test_config_precedence.sh  # environment vs config.sh vs defaults
```

They simulate a second machine by overriding `$HOME` or by pointing two
throwaway KiroCrew directories at a local-directory backend, so they need no
remote storage and touch nothing outside a temporary directory.

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
- Daemon mode with filesystem watching

## License

MIT License - see LICENSE file for details

## Author

Created for syncing KiroCrew data across multiple machines.

Repository: https://github.com/pawelgradziel/kirocrew-sync
