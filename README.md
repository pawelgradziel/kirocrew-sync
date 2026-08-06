# KiroCrew Sync

Cross-platform synchronization utility for KiroCrew data with modular storage backends.

## Features

- 🔄 **Bidirectional sync**: Push and pull your KiroCrew data
- 🔌 **Modular backends**: Google Drive, S3, rsync, or custom
- 🖥️ **Cross-platform**: Works on Linux and macOS
- 🔒 **Safe**: Checks if KiroCrew is running, excludes lock files
- 📦 **Complete**: Syncs all essential data (sessions, knowledge, artifacts, memory)
- 🧭 **Portable paths**: Knowledge folder sources keep working on machines with a different layout

## What Gets Synced

The following KiroCrew data is included in sync:

- **Chat sessions** (`sessions/` directory) - All your conversation history
- **Memory database** (`memory.db`) - Searchable chat history index
- **Memory vector index** (`memory_index.db`) - Semantic search index
- **Session map** (`session_map.json`) - Session metadata
- **Artifacts** (`data/artifacts.db`) - Saved widgets and artifacts
- **Knowledge base** (`workspace/knowledge/`) - Your knowledge library
- **Workspace memory** (`workspace/memory/`) - Workspace-specific memory
- **Learned lessons** (`data/lessons.db`) - Saved corrections and preferences
- **Configuration** (`config.json`) - KiroCrew settings
- **Tags** (`tags.json`) - Tag metadata

## Installation

### Prerequisites

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
machine you own with [rsync](docs/backends/rsync.md), or
[write your own](docs/backends/custom.md) — a backend is one bash file with
three functions.

## Usage

### Basic Commands

```bash
# Push local data to remote storage
./kirocrew-sync.sh push

# Pull remote data to local machine
./kirocrew-sync.sh pull

# Check sync status
./kirocrew-sync.sh status

# Check that knowledge source paths survive a sync
./kirocrew-sync.sh paths

# Show help
./kirocrew-sync.sh help
```

### Switching Between Backends

```bash
# Use Google Drive
SYNC_BACKEND=gdrive ./kirocrew-sync.sh push

# Use S3
SYNC_BACKEND=s3 ./kirocrew-sync.sh push

# Use rsync
SYNC_BACKEND=rsync ./kirocrew-sync.sh push
```

Or edit `config.sh`:
```bash
export SYNC_BACKEND="gdrive"  # or s3, rsync
```

## Typical Workflow

### Setting up two laptops

**On Laptop A (first machine):**
```bash
cd kirocrew-sync
./kirocrew-sync.sh init
# Configure your backend (see Storage Backends above)
./kirocrew-sync.sh push
```

**On Laptop B (second machine):**
```bash
git clone https://github.com/pawelgradziel/kirocrew-sync.git
cd kirocrew-sync
./kirocrew-sync.sh init
# Configure the SAME backend as Laptop A
./kirocrew-sync.sh pull
```

**Daily usage:**
```bash
# Before starting work on Laptop B (get latest from Laptop A)
./kirocrew-sync.sh pull

# After finishing work on Laptop B (share with Laptop A)
./kirocrew-sync.sh push
```

### Automated Sync

You can automate syncing with cron or by adding hooks to your shell profile:

**Option 1: Shell profile hook (automatic)**
```bash
# Add to ~/.bashrc or ~/.zshrc
alias kirocrew='~/.kiro/crew/workspace/kirocrew-sync/kirocrew-sync.sh pull && command kirocrew'
```

**Option 2: Cron job (scheduled)**
```bash
# Edit crontab
crontab -e

# Pull every hour (when KiroCrew is not running)
0 * * * * cd /path/to/kirocrew-sync && ./kirocrew-sync.sh pull 2>&1 | logger -t kirocrew-sync
```

## Knowledge Paths Across Machines

A knowledge folder source is stored as a plain filesystem path. Added on a
laptop, `/home/alice/dev/groover/docs` means nothing on a desktop where the same
repository lives under `/home/alice/work/projects/groover/docs` — the source
goes missing, and with it the entity graph built from those documents.

This tool translates those paths at the sync boundary:

```
push:  /home/alice/dev/groover/docs  ->  ~/dev/groover/docs   (in the bundle)
pull:  ~/dev/groover/docs            ->  /Users/pawel/dev/groover/docs
```

Only the copy in transit is portable. Your local `knowledge.db` always holds
real absolute paths, because KiroCrew resolves source URIs with a bare
`Path(uri)` and does not expand `~` — a tilde stored in the live database would
break every folder source, including on the machine that created it. The
reasoning and the options weighed are recorded in
[ADR 0001](docs/adr/0001-knowledge-path-portability.md).

Three columns travel: the source URI, the per-file ingest state under it, and
the tombstones for auto-discovered project folders. Nothing else is touched.

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

## Safety Features

- **Running check**: Refuses to sync if KiroCrew is currently running
- **Lock file exclusion**: Never syncs `.lock` or `.tmp` files
- **Machine ID tracking**: Each sync includes machine identifier
- **Manifest**: Every sync creates a manifest with checksums and metadata

## Troubleshooting

### "KiroCrew is currently running"
```bash
pkill -f kirocrew
./kirocrew-sync.sh push
```

### Check what will be synced
```bash
./kirocrew-sync.sh status
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
./tests/test_portable_paths.sh   # path translation
./tests/test_sync_paths.sh       # bundle/apply round trip between two machines
```

Both simulate a second machine by overriding `$HOME`, so they need no remote
storage and touch nothing outside a temporary directory.

## Security Considerations

- **OAuth tokens** (Google Drive): Stored in `~/.config/rclone/rclone.conf` (gitignored)
- **AWS credentials**: Stored in `~/.aws/credentials` (never committed)
- **SSH keys** (rsync): Use your existing SSH setup
- **Data in transit**: All backends use encrypted connections (HTTPS/SSH)
- **Local data**: KiroCrew data includes your full chat history and knowledge

**Recommendation:** Use your own OAuth credentials for Google Drive rather than rclone's shared ones.

## Contributing

Contributions welcome! Areas for improvement:

- Additional backends (Dropbox, OneDrive, WebDAV) — see
  [docs/backends/custom.md](docs/backends/custom.md)
- Conflict detection and resolution
- Incremental sync (only changed files)
- Compression before upload
- End-to-end encryption option

## License

MIT License - see LICENSE file for details

## Author

Created for syncing KiroCrew data across multiple machines.

Repository: https://github.com/pawelgradziel/kirocrew-sync
