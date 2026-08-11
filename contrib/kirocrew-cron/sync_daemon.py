"""
Background sync via KiroCrew cron.
Polls for changes and syncs automatically.

Usage:
    kirocrew cron add 'kirocrew-sync' '{"team": false}' \
        --every 300 \
        --script '~/.kiro/crew/crons/sync_daemon.py:sync' \
        --approval-mode auto
"""
import subprocess
import json
from pathlib import Path


def run_sync(ctx):
    """
    Run sync and report results.
    
    Args:
        ctx: KiroCrew cron context with:
            - ctx.message: JSON config with optional "team" boolean
            - ctx.notify(msg): Deliver notification
            - ctx.Skip(): Skip this cycle (nothing to report)
            - ctx.Done(msg): Deliver message and remove job
            - ctx.Report(msg): Deliver message and keep job
    """
    config = {}
    if ctx.message:
        try:
            config = json.loads(ctx.message)
        except json.JSONDecodeError:
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
                # Changes synced - extract summary
                summary_lines = []
                for line in output.split('\n'):
                    if any(marker in line for marker in ['✓', '✗', '⚠', 'Synced', 'Merged', 'Conflict']):
                        summary_lines.append(line)
                
                if summary_lines:
                    msg = "✅ Sync completed\n" + "\n".join(summary_lines[-10:])
                else:
                    msg = "✅ Sync completed"
                
                raise ctx.Report(msg)
        
        elif result.returncode == 3:
            # Quarantine warning
            quarantine_lines = [line for line in output.split('\n') if 'quarantine' in line.lower()]
            msg = "⚠️ Sync completed with quarantine\n" + "\n".join(quarantine_lines[:5])
            raise ctx.Report(msg)
        
        else:
            # Failure - extract error
            error_lines = [line for line in output.split('\n') if '✗' in line or 'error' in line.lower()]
            msg = f"❌ Sync failed (exit {result.returncode})\n" + "\n".join(error_lines[:5])
            raise ctx.Report(msg)
    
    except subprocess.TimeoutExpired:
        raise ctx.Report("⏱️ Sync timed out (>5 minutes)")
    except Exception as e:
        raise ctx.Report(f"❌ Sync error: {e}")


# Entry point for KiroCrew cron
sync = run_sync
