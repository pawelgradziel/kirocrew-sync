# Rsync Backend (Direct Host)

Syncs the bundle straight to a remote server or NAS over SSH. No third-party
service is involved.

## Prerequisites

```bash
# macOS
brew install rsync

# Linux (usually pre-installed)
sudo apt install rsync  # Debian/Ubuntu
sudo yum install rsync  # RHEL/CentOS
```

## Setup

1. **Set up SSH key authentication:**
   ```bash
   ssh-keygen -t ed25519
   ssh-copy-id user@your-remote-host
   ```

2. **Create the remote directory:**
   ```bash
   ssh user@your-remote-host 'mkdir -p /path/to/kirocrew-sync'
   ```

3. **Point the backend at your host** in `backends/rsync.sh`:
   ```bash
   RSYNC_HOST="user@your-remote-host"
   RSYNC_PATH="/path/to/kirocrew-sync"
   ```

4. **Select the backend:**
   ```bash
   export SYNC_BACKEND=rsync
   ./kirocrew-sync.sh push
   ```

   Or set it permanently in `config.sh`:
   ```bash
   export SYNC_BACKEND="rsync"
   ```

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `RSYNC_HOST` | `user@remote-host` | SSH target the bundle is sent to |
| `RSYNC_PATH` | `/path/to/kirocrew-sync` | Remote directory holding the bundle |

Both are read from the environment first, so you can override them in
`config.sh` instead of editing `backends/rsync.sh`.

## Usage

```bash
./kirocrew-sync.sh push     # rsync bundle -> $RSYNC_HOST:$RSYNC_PATH/
./kirocrew-sync.sh pull     # rsync $RSYNC_HOST:$RSYNC_PATH/ -> bundle
./kirocrew-sync.sh status   # host reachability, last sync machine and time
```

Push runs with `--delete`, so the remote directory mirrors the bundle exactly.
`.lock` and `.tmp` files are excluded in both directions, and so is
`$RSYNC_PATH/mailbox/`, the session mailbox.

## Session mailbox

`send-session` and `inbox` (see the README) keep session bundles in
`$RSYNC_PATH/mailbox/<recipient>/`. An upload goes to a hidden temporary name
and is renamed over SSH, so a listing never shows half a file. Listing uses one
SSH round trip and the remote's `stat -c`, so it assumes a GNU userland on the
remote, the same as `backend_list`. The bundles hold whole transcripts; the
permissions advice below applies to them too.

## Troubleshooting

```bash
# Test SSH connection
ssh user@remote-host

# Check remote directory
ssh user@remote-host 'ls -la /path/to/kirocrew-sync'
```

If you get "Cannot access remote host", the backend could not run
`test -d $RSYNC_PATH` over SSH: either the key auth is not working
non-interactively, or the remote directory does not exist yet.

## Security

- Uses your existing SSH keys; nothing extra is stored by this tool.
- Data in transit is encrypted by SSH.
- The bundle contains your full chat history and knowledge base — the remote
  directory should be readable only by your own account.
