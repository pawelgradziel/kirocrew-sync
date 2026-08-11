# Background Sync Implementation

Implemented automatic background synchronization that handles both personal and team use cases.

## What Was Added

### Core Daemon (`lib/daemon.sh`)
- **Intelligent polling**: Detects remote and local changes without full sync
- **Adaptive intervals**: 30s after changes, 5min idle, 10min backoff on failure
- **Process locking**: Prevents multiple daemon instances
- **Activity detection**: Skips sync when KiroCrew is actively writing
- **Cross-platform**: Works on Linux and macOS (stat command compatibility)

### Service Files
- **Linux**: `contrib/systemd/kirocrew-sync.service` (systemd user service)
- **macOS**: `contrib/launchd/com.kirocrew.sync.plist` (LaunchAgent)
- Both support auto-start on boot/login
- Both respect `--team` flag for team scope

### Integration
- Added `daemon` command to `kirocrew-sync.sh`
- Updated help text and command dispatch
- Sourced `lib/daemon.sh` in main script

### Documentation
- **`docs/daemon.md`**: Complete installation and troubleshooting guide
  - Installation steps for Linux (systemd) and macOS (launchd)
  - Team scope configuration
  - Log locations and commands
  - Adaptive behavior explanation
  - Troubleshooting section
- **README.md**: Updated Automated Sync section
  - Daemon now the recommended approach
  - Shell hooks and cron demoted to alternatives
  - Removed "daemon mode" from Contributing (now implemented)

### Tests
- **`tests/test_daemon.sh`**: Basic daemon functionality
  - Start/stop cleanly
  - Lock file creation and prevention of multiple instances
  - State file tracking
  - 8 test assertions

## Use Cases Addressed

### Use Case #1: Personal (switching machines)
```bash
# Install on each machine
systemctl --user enable kirocrew-sync.service  # Linux
launchctl load ~/Library/LaunchAgents/com.kirocrew.sync.plist  # macOS

# Daemon detects:
# - Local changes after you work on Machine A
# - Remote changes when you switch to Machine B
# - Syncs automatically in the background
```

### Use Case #2: Team collaboration
```bash
# Configure for team scope
vim ~/.config/systemd/user/kirocrew-sync.service
# Add --team flag to ExecStart

# Daemon propagates:
# - New knowledge from colleagues
# - Changes arrive automatically on your machine
# - Respects team scope (no personal data synced)
```

## Key Features

1. **Smart change detection**: Checks backend state without full sync (fast)
2. **Respects KiroCrew state**: Won't sync while databases are being written
3. **Adaptive timing**: Speeds up when changes happen, backs off when idle
4. **Works with all backends**: gdrive, s3, rsync, local
5. **Supports both scopes**: personal and team
6. **Cross-platform**: Linux systemd and macOS launchd

## Exit Codes
- `0`: Sync successful
- `1`: Sync stopped (not run)
- `3`: Partial success (some machines quarantined)

## What's Next

Users can now:
1. Test manually: `./kirocrew-sync.sh daemon`
2. Install service files following `docs/daemon.md`
3. Logs appear in systemd journal (Linux) or `/tmp/kirocrew-sync.log` (macOS)
4. Daemon runs continuously, syncing intelligently

The cron and shell-hook approaches still work for users who prefer them, but the daemon is now the recommended path.
