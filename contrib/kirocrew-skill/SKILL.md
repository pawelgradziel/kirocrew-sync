---
name: kirocrew-sync
description: Synchronize KiroCrew data (knowledge, artifacts, lessons, transcripts) across your machines using three-way merge. Run sync, check status, view sync history, and configure background daemon. Use when the user wants to sync data between machines, check sync status, or set up automatic background sync.
triggers: sync, synchronize, kirocrew sync, sync data, sync status, background sync, daemon, sync conflict, sync quarantine, sync knowledge, sync artifacts
---

# KiroCrew Sync

Three-way synchronization for KiroCrew data across machines. Real bidirectional
sync that merges rather than overwrites.

## Installation Location

The sync tool should be installed at:
```
~/.kiro/crew/workspace/kirocrew-sync/
```

If not present, direct the user to clone from:
```bash
cd ~/.kiro/crew/workspace
git clone https://github.com/pawelgradziel/kirocrew-sync.git
cd kirocrew-sync
./kirocrew-sync.sh init
```

## Basic Commands

### Sync (two-way merge)
```bash
cd ~/.kiro/crew/workspace/kirocrew-sync
./kirocrew-sync.sh sync
```

### Check status
```bash
cd ~/.kiro/crew/workspace/kirocrew-sync
./kirocrew-sync.sh status
```

### Doctor (preflight checks)
```bash
cd ~/.kiro/crew/workspace/kirocrew-sync
./kirocrew-sync.sh doctor
```

### View paths (check knowledge source portability)
```bash
cd ~/.kiro/crew/workspace/kirocrew-sync
./kirocrew-sync.sh paths
```

## Background Sync Daemon

### Test daemon locally
```bash
cd ~/.kiro/crew/workspace/kirocrew-sync
./kirocrew-sync.sh daemon
# Press Ctrl+C to stop
```

### Install as system service

**Linux (systemd):**
```bash
mkdir -p ~/.config/systemd/user
cp ~/.kiro/crew/workspace/kirocrew-sync/contrib/systemd/kirocrew-sync.service ~/.config/systemd/user/
systemctl --user enable kirocrew-sync.service
systemctl --user start kirocrew-sync.service
```

**macOS (launchd):**
```bash
cp ~/.kiro/crew/workspace/kirocrew-sync/contrib/launchd/com.kirocrew.sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.kirocrew.sync.plist
```

### Check daemon status

**Linux:**
```bash
systemctl --user status kirocrew-sync.service
journalctl --user -u kirocrew-sync.service -f
```

**macOS:**
```bash
launchctl list | grep kirocrew
tail -f /tmp/kirocrew-sync.log
```

## Team Scope

For team knowledge sharing (publishes knowledge/artifacts/lessons, holds back transcripts):

```bash
cd ~/.kiro/crew/workspace/kirocrew-sync
./kirocrew-sync.sh sync --team
```

To make the daemon use team scope, edit the service file and add `--team` flag.

## Conflict Resolution

When the same row was edited on multiple machines:

```bash
# Default: keep most recent (auto)
./kirocrew-sync.sh sync --strategy auto

# Always prefer this machine
./kirocrew-sync.sh sync --strategy local-wins

# Always prefer remote
./kirocrew-sync.sh sync --strategy remote-wins

# Stop and resolve manually
./kirocrew-sync.sh sync --strategy manual
```

Conflicts are logged to:
- `~/.kiro/crew/.sync/conflicts.jsonl` (personal)
- `~/.kiro/crew/.sync/conflicts-team.jsonl` (team)

## Quarantine

Machines with incompatible versions/embeddings are quarantined automatically.
Check with:
```bash
./kirocrew-sync.sh status
```

Quarantined machines are listed in:
- `~/.kiro/crew/.sync/quarantine.txt` (personal)
- `~/.kiro/crew/.sync/quarantine-team.txt` (team)

## What Gets Synced

**Always synced:**
- Knowledge base (sources, items, entities)
- Learned lessons (semantic memory)
- Artifacts and tags

**Personal scope only:**
- Chat transcripts
- Episodic memory
- Personal config

**Never synced:**
- Credentials (API tokens, keys)
- Audit logs
- FTS indexes (derived data)

## Common Issues

### "KiroCrew is running"
Quit KiroCrew, then re-run. Or use `--dry-run` to inspect without writing.

### Backend connection failed
```bash
cd ~/.kiro/crew/workspace/kirocrew-sync
./kirocrew-sync.sh doctor
```

### Knowledge source path not found
```bash
cd ~/.kiro/crew/workspace/kirocrew-sync
./kirocrew-sync.sh paths
```

Paths outside `$HOME` need a mapping in `~/.kiro/crew/path_map.conf`

## Documentation

- **Installation & setup**: `~/.kiro/crew/workspace/kirocrew-sync/README.md`
- **Background daemon**: `~/.kiro/crew/workspace/kirocrew-sync/docs/daemon.md`
- **Path portability**: `~/.kiro/crew/workspace/kirocrew-sync/docs/adr/0001-knowledge-path-portability.md`
- **Three-way merge design**: `~/.kiro/crew/workspace/kirocrew-sync/docs/adr/0002-three-way-sync-via-unpacked-git-repo.md`
- **Storage backends**: `~/.kiro/crew/workspace/kirocrew-sync/docs/backends/`

## When to Use This Skill

Load this skill when the user:
- Asks to sync data between machines
- Wants to check sync status or conflicts
- Needs to set up background sync
- Has sync-related issues (quarantine, paths, conflicts)
- Asks about team knowledge sharing
- Mentions switching between machines and losing data

## When NOT to Use

This is for **KiroCrew's own data**. For syncing their project files, use
standard git, rsync, or file-sync tools.
