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

Install the storage backend tools you plan to use:

**For Google Drive backend:**
```bash
# macOS
brew install rclone

# Linux
curl https://rclone.org/install.sh | sudo bash
```

**For S3 backend:**
```bash
# macOS
brew install awscli

# Linux
pip install awscli
```

**For rsync backend:**
```bash
# macOS
brew install rsync

# Linux (usually pre-installed)
sudo apt install rsync  # Debian/Ubuntu
sudo yum install rsync  # RHEL/CentOS
```

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

3. **Configure your storage backend** (see sections below)

## Storage Backends

### Google Drive (Recommended)

Uses [rclone](https://rclone.org/) to sync with Google Drive.

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

### AWS S3

Uses AWS CLI to sync with S3 bucket.

#### Setup Instructions

1. **Install and configure AWS CLI:**
   ```bash
   aws configure
   ```
   Enter your AWS Access Key ID, Secret Access Key, and default region.

2. **Create an S3 bucket:**
   ```bash
   aws s3 mb s3://your-kirocrew-sync-bucket
   ```

3. **Enable versioning (recommended):**
   ```bash
   aws s3api put-bucket-versioning \
     --bucket your-kirocrew-sync-bucket \
     --versioning-configuration Status=Enabled
   ```

4. **Edit the S3 backend configuration:**
   ```bash
   nano backends/s3.sh
   ```
   Update:
   ```bash
   S3_BUCKET="your-kirocrew-sync-bucket"
   AWS_PROFILE="default"  # or your profile name
   ```

5. **Set backend to S3:**
   ```bash
   export SYNC_BACKEND=s3
   ./kirocrew-sync.sh push
   ```

### Rsync (Direct Host)

Sync directly to a remote server or NAS via SSH.

#### Setup Instructions

1. **Set up SSH key authentication:**
   ```bash
   ssh-keygen -t ed25519
   ssh-copy-id user@your-remote-host
   ```

2. **Create remote directory:**
   ```bash
   ssh user@your-remote-host 'mkdir -p /path/to/kirocrew-sync'
   ```

3. **Edit the rsync backend configuration:**
   ```bash
   nano backends/rsync.sh
   ```
   Update:
   ```bash
   RSYNC_HOST="user@your-remote-host"
   RSYNC_PATH="/path/to/kirocrew-sync"
   ```

4. **Set backend to rsync:**
   ```bash
   export SYNC_BACKEND=rsync
   ./kirocrew-sync.sh push
   ```

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

Or edit `config.sh` to set the backend for every run:
```bash
export SYNC_BACKEND="gdrive"  # or s3, rsync
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
# Configure your backend (see sections above)
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

### Backend-specific issues

**Google Drive:**
```bash
# Re-authenticate
rclone config reconnect kirocrew-gdrive

# Test connection
rclone lsd kirocrew-gdrive:
```

**S3:**
```bash
# Check AWS credentials
aws sts get-caller-identity

# List bucket contents
aws s3 ls s3://your-bucket/kirocrew-sync/
```

**Rsync:**
```bash
# Test SSH connection
ssh user@remote-host

# Check remote directory
ssh user@remote-host 'ls -la /path/to/kirocrew-sync'
```

## Creating Custom Backends

To add a new storage backend:

1. Create `backends/your-backend.sh`
2. Implement these functions:
   ```bash
   backend_push() {
       local bundle_dir="$1"
       # Upload bundle_dir contents to your storage
   }

   backend_pull() {
       local bundle_dir="$1"
       # Download from your storage to bundle_dir
   }

   backend_status() {
       # Show backend status and configuration
   }
   ```

3. Use it:
   ```bash
   SYNC_BACKEND=your-backend ./kirocrew-sync.sh push
   ```

See existing backends for examples.

## Tests

```bash
./tests/test_portable_paths.sh     # path translation
./tests/test_sync_paths.sh         # bundle/apply round trip between two machines
./tests/test_config_precedence.sh  # environment vs config.sh vs defaults
```

They simulate a second machine by overriding `$HOME`, so they need no remote
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

- Additional backends (Dropbox, OneDrive, WebDAV)
- Conflict detection and resolution
- Incremental sync (only changed files)
- Compression before upload
- End-to-end encryption option

## License

MIT License - see LICENSE file for details

## Author

Created for syncing KiroCrew data across multiple machines.

Repository: https://github.com/pawelgradziel/kirocrew-sync
