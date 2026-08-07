# Quick Start Guide

Get KiroCrew syncing between your laptops in 5 minutes.

One command, `sync`, merges changes in **both** directions — you never have to
work out which laptop has newer data.

## Step 1: Install rclone

**macOS:**
```bash
brew install rclone
```

**Linux:**
```bash
curl https://rclone.org/install.sh | sudo bash
```

(Python 3 and git are also required, and are already installed on macOS and
most Linux distributions.)

## Step 2: Clone and initialize

```bash
git clone https://github.com/pawelgradziel/kirocrew-sync.git
cd kirocrew-sync
./kirocrew-sync.sh init
```

## Step 3: Configure Google Drive

```bash
rclone config
```

- Choose: `n` (New remote)
- Name: `kirocrew-gdrive`
- Type: `drive` (Google Drive)
- Scope: `drive` (Full access)
- Client ID/Secret: Leave blank (or add your own - see README)
- Complete browser authorization

## Step 4: Check everything looks right

```bash
./kirocrew-sync.sh doctor
```

This shows what will be synced and what is deliberately excluded. Quit the
KiroCrew app before the next step.

## Step 5: First sync

**On your current laptop:**
```bash
./kirocrew-sync.sh sync
```

**On your other laptop:**
```bash
git clone https://github.com/pawelgradziel/kirocrew-sync.git
cd kirocrew-sync
./kirocrew-sync.sh init
rclone config  # Use the SAME settings as above
./kirocrew-sync.sh sync
```

## Step 6: Check your knowledge sources

```bash
./kirocrew-sync.sh paths
```

Knowledge folder sources are stored as filesystem paths, so a source added on
one laptop has to find its folder on the other. Anything under your home
directory travels automatically (`~/code/docs` works on both). Folders outside
`$HOME`, or repos kept under different subdirectories on each machine, need one
line per machine in `~/.kiro/crew/path_map.conf` — see
[README.md](README.md#knowledge-paths-across-machines).

## Done!

## Daily Usage

```bash
# Whichever laptop you sit down at, and again when you finish
./kirocrew-sync.sh sync
```

That is the whole workflow. Changes made on both machines are merged, not
overwritten: a lesson learned on one laptop and a knowledge item added on the
other both survive, because they are different rows.

If the *same* entry was edited on both machines, the most recently updated one
wins by default and the choice is reported. To decide differently:

```bash
./kirocrew-sync.sh sync --strategy local-wins    # or remote-wins, or manual
```

## What's Synced

✅ Chat history
✅ Knowledge base
✅ Artifacts
✅ Memory and learned lessons
✅ Configuration

Not synced, on purpose: API tokens and credentials, local audit logs, and
search indexes (rebuilt automatically after each sync).

## Troubleshooting

**"KiroCrew is running" error:**
Quit the KiroCrew app. To look around without stopping it:
```bash
./kirocrew-sync.sh sync --dry-run
```

**Check what will sync:**
```bash
./kirocrew-sync.sh doctor
```

**Test Google Drive connection:**
```bash
rclone lsd kirocrew-gdrive:
```

See [README.md](README.md) for full documentation, other storage backends
(S3, rsync, local folder), and conflict handling. The design and its trade-offs
are written up in [ADR 0002](docs/adr/0002-three-way-sync-via-unpacked-git-repo.md).
