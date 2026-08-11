# KiroCrew Integration

Multiple ways to integrate kirocrew-sync into the KiroCrew interface.

## 1. Skill Integration (Recommended)

**What it does**: Makes sync discoverable and actionable from natural language chat.

**Installation**:
```bash
# Copy the skill to KiroCrew's skills directory
cp contrib/kirocrew-skill/SKILL.md ~/.kiro/crew/skills/kirocrew-sync/SKILL.md
```

**Usage**: Users can then say things like:
- "sync my data"
- "check sync status"
- "set up background sync"
- "why is sync quarantining my laptop?"

KiroCrew will automatically load the skill when these triggers match and execute the appropriate sync commands.

**Pros**:
- Natural language interface
- Contextual help
- No manual command memorization
- Integrates with KiroCrew's agent workflow

**Cons**:
- Requires user to ask explicitly (not automatic)

---

## 2. KiroCrew Cron Integration (Automatic Background)

**What it does**: Uses KiroCrew's built-in cron scheduler to run sync automatically.

**Why use this over systemd/launchd**: 
- Cross-platform (works on any OS KiroCrew runs on)
- Managed through KiroCrew dashboard (list, pause, resume, logs)
- Can notify through KiroCrew's notification system
- Respects KiroCrew's resource management

**Installation**:

### Option A: Script cron (deterministic, zero-token)

```bash
# Create the sync script
mkdir -p ~/.kiro/crew/crons
cat > ~/.kiro/crew/crons/sync_daemon.py << 'EOF'
"""
Background sync via KiroCrew cron.
Polls for changes and syncs automatically.
"""
import subprocess
import json
from pathlib import Path

def run_sync(ctx):
    """
    Run sync and report results.
    ctx.message contains optional config as JSON.
    """
    config = {}
    if ctx.message:
        try:
            config = json.loads(ctx.message)
        except:
            pass
    
    sync_dir = Path.home() / ".kiro/crew/workspace/kirocrew-sync"
    
    if not sync_dir.exists():
        raise ctx.Report(
            "⚠️ kirocrew-sync not installed at ~/.kiro/crew/workspace/kirocrew-sync\n"
            "Clone from: https://github.com/pawelgradziel/kirocrew-sync"
        )
    
    # Build command
    cmd = [str(sync_dir / "kirocrew-sync.sh"), "sync", "--strategy", "auto"]
    
    if config.get("team", False):
        cmd.append("--team")
    
    # Run sync
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(sync_dir)
        )
        
        # Parse output for meaningful events
        output = result.stdout + result.stderr
        
        if result.returncode == 0:
            # Check if anything actually synced
            if "No changes" in output or "up to date" in output:
                raise ctx.Skip()  # Nothing to report, try again next cycle
            else:
                # Changes synced
                raise ctx.Report(f"✅ Sync completed\n{output[-500:]}")
        
        elif result.returncode == 3:
            # Quarantine warning
            raise ctx.Report(f"⚠️ Sync completed with quarantine\n{output[-500:]}")
        
        else:
            # Failure
            raise ctx.Report(f"❌ Sync failed (exit {result.returncode})\n{output[-500:]}")
    
    except subprocess.TimeoutExpired:
        raise ctx.Report("⏱️ Sync timed out (>5 minutes)")
    except Exception as e:
        raise ctx.Report(f"❌ Sync error: {e}")

# Entry point for KiroCrew cron
sync = run_sync
EOF

# Register the cron (every 5 minutes)
kirocrew cron add \
  'kirocrew-sync-daemon' \
  '{"team": false}' \
  --every 300 \
  --script '~/.kiro/crew/crons/sync_daemon.py:sync' \
  --approval-mode auto
```

### Option B: LLM cron (flexible, uses tokens)

```bash
kirocrew cron add \
  'kirocrew-sync-auto' \
  'cd ~/.kiro/crew/workspace/kirocrew-sync && ./kirocrew-sync.sh sync --strategy auto. If exit code is 0 or 3, report only if changes were synced or machines quarantined. If exit code is 1, report the error.' \
  --every 300 \
  --approval-mode auto
```

**Checking status**:
```bash
kirocrew cron list
kirocrew cron pause <job-id>
kirocrew cron resume <job-id>
```

**Pros**:
- Cross-platform
- Dashboard visibility
- Integrated with KiroCrew notifications
- Can be paused/resumed from dashboard

**Cons**:
- Polling interval is fixed (not adaptive like standalone daemon)
- LLM cron costs tokens per run (script cron is free)

---

## 3. Systemd/Launchd Daemon (OS-Level)

**What it does**: Native OS service that polls intelligently with adaptive intervals.

See `docs/daemon.md` for full installation.

**Pros**:
- Adaptive intervals (30s when active, 5min idle)
- Runs even when KiroCrew dashboard is closed
- Most efficient (no overhead between cycles)

**Cons**:
- Platform-specific (Linux or macOS)
- Separate from KiroCrew's management
- Logs in system journal, not dashboard

---

## 4. Custom MCP Tool (Future)

**What it would do**: Expose sync operations as MCP tools that agents can call directly.

**Not yet implemented** — would require:
- MCP server wrapping `kirocrew-sync.sh`
- Tool definitions for `sync`, `status`, `doctor`, `paths`
- Registration in `~/.kiro/crew/mcp.json`

This would let any KiroCrew agent call sync tools without going through bash, and could enable richer integrations like:
- Dashboard status widget showing last sync time
- Notification when remote changes are available
- Auto-sync before/after certain operations

---

## Comparison

| Integration | Automatic | Adaptive | Dashboard UI | Cross-platform | Token Cost |
|-------------|-----------|----------|--------------|----------------|------------|
| **Skill** | ❌ Manual | N/A | Chat only | ✅ | Per use |
| **Cron (script)** | ✅ | ❌ Fixed | ✅ Jobs list | ✅ | Zero |
| **Cron (LLM)** | ✅ | ❌ Fixed | ✅ Jobs list | ✅ | Per cycle |
| **systemd/launchd** | ✅ | ✅ Smart | ❌ | ⚠️ Linux/macOS | Zero |
| **MCP tool** | Depends | Depends | Could have widget | ✅ | Depends |

## Recommended Setup

**For single-user, 2-3 machines**:
- Install the **skill** for discoverability
- Use **systemd/launchd daemon** for automatic sync
- User can still ask "check sync status" in chat

**For team use**:
- Install the **skill** on each machine
- Use **KiroCrew script cron** with team scope
- Visible in dashboard, can pause during maintenance

**For development/testing**:
- Just the **skill** — run sync manually when needed
- Or systemd daemon for live testing

---

## Installation Quick Start

### Minimal (skill only):
```bash
mkdir -p ~/.kiro/crew/skills/kirocrew-sync
cp contrib/kirocrew-skill/SKILL.md ~/.kiro/crew/skills/kirocrew-sync/SKILL.md
```

### Recommended (skill + daemon):
```bash
# Install skill
mkdir -p ~/.kiro/crew/skills/kirocrew-sync
cp contrib/kirocrew-skill/SKILL.md ~/.kiro/crew/skills/kirocrew-sync/SKILL.md

# Install daemon (Linux)
mkdir -p ~/.config/systemd/user
cp contrib/systemd/kirocrew-sync.service ~/.config/systemd/user/
systemctl --user enable kirocrew-sync.service
systemctl --user start kirocrew-sync.service

# Or daemon (macOS)
cp contrib/launchd/com.kirocrew.sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.kirocrew.sync.plist
```

### Team (skill + cron):
```bash
# Install skill
mkdir -p ~/.kiro/crew/skills/kirocrew-sync
cp contrib/kirocrew-skill/SKILL.md ~/.kiro/crew/skills/kirocrew-sync/SKILL.md

# Install script
mkdir -p ~/.kiro/crew/crons
cp contrib/kirocrew-cron/sync_daemon.py ~/.kiro/crew/crons/

# Register with team scope
kirocrew cron add \
  'kirocrew-sync-team' \
  '{"team": true}' \
  --every 300 \
  --script '~/.kiro/crew/crons/sync_daemon.py:sync' \
  --approval-mode auto
```
