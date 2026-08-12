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

The 5/0.5/10-minute figures above are the *standalone* defaults. If the
KiroCrew Sync app is installed, this daemon cooperates with it instead --
see [KiroCrew App Integration](#kirocrew-app-integration) below for what
changes.

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

## KiroCrew App Integration

This daemon works standalone -- nothing below is required for
`./kirocrew-sync.sh daemon` to function, and everything above this section
still applies unchanged if you never install the KiroCrew Sync app. This
section describes what changes when the app *is* installed.

### Why this exists

Without the app, `kirocrew-sync.sh daemon` running via cron/systemd/launchd
is invisible to KiroCrew: it calls `kirocrew-sync.sh sync` directly, which
records nothing anywhere the app can see (no history row, no conflict/
quarantine entry, no notification), and each run truncates
`conflicts.jsonl`/`quarantine.txt` as its first action -- so even that
evidence is gone before anything else could read it. A machine syncing
purely through this daemon would show an empty dashboard despite syncing
being fully functional. And separately, the app's dashboard lets you set a
polling interval and scope, but this daemon used to have no way to know
about either -- they were just a database row nobody read.

### Detection

Every daemon tick checks whether the app is installed, by looking for its
standalone entry point at the layout the app itself uses (mirroring
`app.json`'s own cron command and `app/backend/sync_runner.py`'s
`resolve_db_path()`, rather than inventing a second scheme):

```
$KIROCREW_DIR/apps/kirocrew-sync/backend/cli.py
$KIROCREW_DIR/apps/kirocrew-sync/.venv/bin/python3
```

If both exist and the Python interpreter is executable, the app is
considered installed for that tick. This is re-checked every cycle, not
just at startup, so installing or removing the app while the daemon is
already running takes effect on the next tick.

### Recording: delegating to `cli.py`

When the app is installed, a tick that decides to sync no longer calls
`kirocrew-sync.sh sync` directly. It instead runs:

```
<app dir>/.venv/bin/python3 <app dir>/backend/cli.py --strategy auto [--team]
```

(`--team` is added when this daemon's own scope is `team`, i.e. it was
started with `kirocrew-sync.sh daemon --team`.) `cli.py` is the app's own
entry point for exactly this case -- it calls this same engine and then
records a `sync_runs` row, ingests any conflicts/quarantine, and sends
notifications, the same as clicking "Sync Now" in the dashboard. See
`app/backend/cli.py` and `app/backend/sync_runner.py` for that side of the
contract. This daemon does not reimplement any of that recording itself.

`cli.py` exits 0 for a clean sync *or* one that ends with machines still
quarantined (engine exit 3 -- its own contract deliberately treats that as
"the cron did its job"), and non-zero on a real failure. This daemon
recovers the underlying engine exit code from `cli.py`'s own summary log
line when present, so a quarantined run still gets the same "back to the
idle cadence" treatment (rather than the faster "something happened, check
again soon" one) it would get without the app installed -- and falls back
to `cli.py`'s own 0/non-zero exit code if that line is ever missing.

Note this app also has its own, separate delivery mechanism: `app.json`
declares a KiroCrew-native cron job that ticks `cli.py` directly, on its
own fixed schedule, independent of whether this daemon is running at all.
Running this daemon *in addition to* an installed app is safe, not
redundant-and-wrong: `cli.py`'s own `run_sync_and_record()` checks whether a
sync is already in flight before starting another, so an app-cron tick and
a daemon tick landing close together do not race.

### Interval reconciliation

The app's dashboard exposes a single "how often" number (60-900 seconds,
stored in its `daemon_state` table). This daemon has three: idle, active,
backoff. Collapsing to one flat interval everywhere would throw the
adaptive behavior away entirely -- no more "check back in 30 seconds after
something happened", no more "back off for longer after a failure". So
instead:

- The app's configured interval becomes the new **idle** baseline (that is
  what "how often should this poll" means when nothing is happening).
- **Active** and **backoff** are rescaled around it, keeping the same
  ratios as the hardcoded defaults: active = idle ÷ 10, backoff = idle × 2
  (so the defaults' 300/30/600 relationship is preserved at any configured
  idle value).

A user who sets the slider to, say, 600s gets idle=600s, active=60s,
backoff=1200s -- proportionally faster follow-up checks and proportionally
longer backoff, not a flat 600s everywhere. This is re-read every cycle, so
a change made on the dashboard while the daemon is already running takes
effect on the next tick, without a restart.

If the app isn't installed, or its interval can't be read (see below),
this daemon falls straight back to the hardcoded 300/30/600 defaults --
identical to running without the app at all.

### The `sqlite3` requirement

Reading the app's configured interval means reading its sqlite database
from bash, which needs the `sqlite3` CLI binary. This is checked with
`command -v sqlite3` on first use and cached for the process's lifetime;
its absence is handled gracefully, not assumed away. If `sqlite3` isn't
installed:

- The app can still be detected and ticks still delegate to `cli.py` (that
  path needs only the app's own Python/venv, never the `sqlite3` binary --
  `cli.py` talks to its database through Python's own `sqlite3` module, in
  its own process).
- Only interval reading is affected: this daemon falls back to the
  hardcoded 300/30/600 defaults, and logs that fact once at startup.

```
[...] KiroCrew app detected at /home/user/.kiro/crew/apps/kirocrew-sync: sync ticks will be delegated to its cli.py for recording
[...] sqlite3 not found: using built-in adaptive intervals (cannot read the app's configured interval)
```

### Database location

The database read for the interval is resolved the same way
`sync_runner.resolve_db_path()` resolves it on the app's side: an explicit
`KIROCREW_SYNC_DB` environment variable wins; otherwise it's
`$KIROCREW_DIR/apps/kirocrew-sync/data/history.db`.

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

If the KiroCrew app is installed, its dashboard (Settings → Daemon) also
controls the polling interval -- see
[KiroCrew App Integration](#kirocrew-app-integration) above. That in turn
honors `KIROCREW_SYNC_DB` if set, the same escape hatch the app itself
supports for a non-default database location.

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

The "5 minutes" figure is the *idle* interval; if the KiroCrew app is
installed and its dashboard interval is readable (see
[KiroCrew App Integration](#kirocrew-app-integration)), it replaces 5
minutes as the idle baseline and the other two intervals are rescaled
around it, not fixed at 30s/10min regardless of the configured value.

## How idle detection works

The "No changes detected" row above depends on a fast, no-merge check
against the storage backend: each backend implements `backend_list()`, a
cheap fingerprint of the remote's published bundles (name, size, and
modification time, sorted deterministically) that changes if and only if
another machine has published since the last check. See
[Custom Backends](backends/custom.md) for the exact contract, including
how a backend distinguishes "reachable but nothing published yet" from
"can't reach the backend at all" -- only the latter falls back to the
"Sync failed or conflicted" backoff cadence instead of settling into idle.

If you see your daemon logging "Backend unreachable" continuously even
though the backend is healthy and reachable by other means
(`kirocrew-sync.sh status` works, manual `sync` works), that points at
`backend_list()` for your configured `SYNC_BACKEND` specifically -- check
that the same credentials/tool (`rclone`/`aws`/`ssh`) it uses are
available in the daemon's environment, which can differ from an
interactive shell's (cron and systemd/launchd services often start with a
minimal `PATH` and no SSH agent).

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
