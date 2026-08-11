# Background Sync Daemon

Automatic background synchronization for both personal and team use cases.

## How It Works

The daemon runs continuously in the background, intelligently polling for changes:

- **Remote changes detected** → sync immediately, then check again in 30 seconds
- **No changes** → sleep for 5 minutes before next check
- **Sync failure** → back off for 10 minutes, then retry
- **KiroCrew active** → defer sync until databases are idle

This handles both use cases:
1. **Personal**: switching between your own machines
2. **Team**: colleagues publishing new knowledge that propagates to your machine

## Installation

### Linux (systemd)

1. **Copy the service file:**
   ```bash
   mkdir -p ~/.config/systemd/user
   cp contrib/systemd/kirocrew-sync.service ~/.config/systemd/user/
   ```

2. **Edit paths if needed** (if kirocrew-sync is not in the default location):
   ```bash
   vim ~/.config/systemd/user/kirocrew-sync.service
   # Change ExecStart path if your installation differs
   ```

3. **Enable and start:**
   ```bash
   systemctl --user enable kirocrew-sync.service
   systemctl --user start kirocrew-sync.service
   ```

4. **Check status:**
   ```bash
   systemctl --user status kirocrew-sync.service
   journalctl --user -u kirocrew-sync.service -f
   ```

5. **Auto-start on boot:**
   ```bash
   sudo loginctl enable-linger $USER
   ```

### macOS (launchd)

1. **Copy the LaunchAgent plist:**
   ```bash
   cp contrib/launchd/com.kirocrew.sync.plist ~/Library/LaunchAgents/
   ```

2. **Edit paths if needed:**
   ```bash
   vim ~/Library/LaunchAgents/com.kirocrew.sync.plist
   # Adjust the path in ProgramArguments if your installation differs
   ```

3. **Load and start:**
   ```bash
   launchctl load ~/Library/LaunchAgents/com.kirocrew.sync.plist
   launchctl start com.kirocrew.sync
   ```

4. **Check status:**
   ```bash
   launchctl list | grep kirocrew
   tail -f /tmp/kirocrew-sync.log
   ```

5. **Auto-start on login** is automatic with LaunchAgents

## Manual Testing

Before enabling the daemon, test it manually:

```bash
./kirocrew-sync.sh daemon
```

Press Ctrl+C to stop. You should see:
```
[2026-08-11 21:45:00] KiroCrew Sync daemon starting (scope: personal)
[2026-08-11 21:45:00] No changes detected, sleeping...
[2026-08-11 21:45:00] Next check in 300s
```

## Team Scope

To run background sync in team mode, edit the service file:

**Linux (systemd):**
```bash
vim ~/.config/systemd/user/kirocrew-sync.service
```
Change `ExecStart` line to:
```
ExecStart=%h/.kiro/crew/workspace/kirocrew-sync/kirocrew-sync.sh daemon --team
```

**macOS (launchd):**
```bash
vim ~/Library/LaunchAgents/com.kirocrew.sync.plist
```
Change the command string to:
```xml
<string>~/.kiro/crew/workspace/kirocrew-sync/kirocrew-sync.sh daemon --team</string>
```

Then reload:
```bash
# Linux
systemctl --user daemon-reload
systemctl --user restart kirocrew-sync.service

# macOS
launchctl unload ~/Library/LaunchAgents/com.kirocrew.sync.plist
launchctl load ~/Library/LaunchAgents/com.kirocrew.sync.plist
```

## Configuration

The daemon respects your `config.sh` settings:
- `SYNC_BACKEND` (gdrive, s3, rsync, local)
- `KIROCREW_DIR`
- `SYNC_SCOPE` (personal or team)

Environment variables in the service file override `config.sh` if needed.

## Logs

**Linux:**
```bash
journalctl --user -u kirocrew-sync.service -f       # Follow live
journalctl --user -u kirocrew-sync.service -n 100   # Last 100 lines
```

**macOS:**
```bash
tail -f /tmp/kirocrew-sync.log           # Normal output
tail -f /tmp/kirocrew-sync.error.log     # Errors only
```

## Stopping the Daemon

**Linux:**
```bash
systemctl --user stop kirocrew-sync.service
systemctl --user disable kirocrew-sync.service  # Prevent auto-start
```

**macOS:**
```bash
launchctl stop com.kirocrew.sync
launchctl unload ~/Library/LaunchAgents/com.kirocrew.sync.plist  # Prevent auto-start
```

## Troubleshooting

### Daemon not starting

**Check the lock file:**
```bash
rm ~/.kiro/crew/.sync/daemon.lock
```

**Verify backend access:**
```bash
./kirocrew-sync.sh status
```

### High CPU usage

The daemon is designed to be idle most of the time. If `ps` shows constant CPU:
- Check logs for repeated sync failures
- Ensure backend credentials are valid
- Try `./kirocrew-sync.sh doctor`

### Conflicts or quarantine

The daemon uses `--strategy auto` (most recent wins). Manual conflicts are logged to:
```
~/.kiro/crew/.sync/conflicts.jsonl        # Personal scope
~/.kiro/crew/.sync/conflicts-team.jsonl   # Team scope
```

Quarantined machines (version/model mismatch) are listed in:
```
~/.kiro/crew/.sync/quarantine.txt         # Personal scope
~/.kiro/crew/.sync/quarantine-team.txt    # Team scope
```

## Adaptive Behavior

The daemon adjusts its polling based on activity:

| Situation | Interval | Why |
|-----------|----------|-----|
| No changes detected | 5 minutes | Save battery, reduce backend requests |
| Changes synced successfully | 30 seconds | Catch follow-on changes quickly |
| Sync failed or conflicted | 10 minutes | Back off, give time to resolve |
| KiroCrew actively writing | Skip cycle | Avoid mid-write corruption |

## Alternative: Cron (simpler, less adaptive)

If you prefer a fixed schedule without adaptive intervals:

```bash
crontab -e
```

Add one of:
```bash
# Every 10 minutes
*/10 * * * * cd ~/.kiro/crew/workspace/kirocrew-sync && ./kirocrew-sync.sh sync 2>&1 | logger -t kirocrew-sync

# On the hour
0 * * * * cd ~/.kiro/crew/workspace/kirocrew-sync && ./kirocrew-sync.sh sync 2>&1 | logger -t kirocrew-sync
```

This is simpler but won't adapt to activity or skip when KiroCrew is busy.
