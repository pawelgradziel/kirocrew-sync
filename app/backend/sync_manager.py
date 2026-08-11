"""
Sync manager - wrapper around kirocrew-sync.sh bash script.

Executes sync, parses output, and returns structured results.
"""

import subprocess
import re
import time
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime, timedelta

from .models import SyncResult, SyncChange, SyncStatus


class SyncManager:
    """Manages sync operations via bash script."""
    
    def __init__(self):
        # Default to workspace location
        self.sync_dir = Path.home() / ".kiro/crew/workspace/kirocrew-sync"
        self.script = self.sync_dir / "kirocrew-sync.sh"
        
        if not self.script.exists():
            raise FileNotFoundError(
                f"kirocrew-sync.sh not found at {self.script}. "
                "Install from: https://github.com/pawelgradziel/kirocrew-sync"
            )
    
    def is_running(self) -> bool:
        """Check if sync is currently running."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "kirocrew-sync.sh sync"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
    
    def run_sync(
        self,
        strategy: str = "auto",
        team: bool = False,
        dry_run: bool = False,
        timeout: int = 300
    ) -> SyncResult:
        """
        Run sync and parse results.
        
        Args:
            strategy: Conflict resolution strategy (auto, local-wins, remote-wins, manual)
            team: Use team scope instead of personal
            dry_run: Run without writing changes
            timeout: Command timeout in seconds
        
        Returns:
            SyncResult with parsed output
        
        Raises:
            subprocess.TimeoutExpired: If sync takes longer than timeout
            subprocess.CalledProcessError: If sync script not found
        """
        start_time = time.time()
        
        # Build command
        cmd = [str(self.script), "sync", "--strategy", strategy]
        if team:
            cmd.append("--team")
        if dry_run:
            cmd.append("--dry-run")
        
        # Run sync
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(self.sync_dir)
        )
        
        duration_ms = int((time.time() - start_time) * 1000)
        
        # Parse output
        output = result.stdout + result.stderr
        parsed = self._parse_output(output, result.returncode)
        
        return SyncResult(
            exit_code=result.returncode,
            duration_ms=duration_ms,
            scope="team" if team else "personal",
            strategy=strategy,
            dry_run=dry_run,
            changes_detected=parsed["changes_detected"],
            rows_merged=parsed["rows_merged"],
            conflicts_count=parsed["conflicts_count"],
            quarantine_count=parsed["quarantine_count"],
            output=output,
            error=result.stderr if result.returncode != 0 else None
        )
    
    def _parse_output(self, output: str, exit_code: int) -> dict:
        """
        Parse sync output for structured data.
        
        Looks for patterns like:
        - "✓ Synced N tables, M rows"
        - "⚠ N conflict(s) resolved"
        - "⚠ N machine(s) quarantined"
        - "No changes detected"
        """
        parsed = {
            "changes_detected": False,
            "rows_merged": 0,
            "conflicts_count": 0,
            "quarantine_count": 0
        }
        
        # No changes
        if "No changes" in output or "up to date" in output:
            return parsed
        
        # Changes detected
        if exit_code in (0, 3):
            parsed["changes_detected"] = True
        
        # Extract row count
        rows_match = re.search(r"(\d+)\s+rows?", output, re.IGNORECASE)
        if rows_match:
            parsed["rows_merged"] = int(rows_match.group(1))
        
        # Extract conflicts
        conflict_match = re.search(r"(\d+)\s+conflict", output, re.IGNORECASE)
        if conflict_match:
            parsed["conflicts_count"] = int(conflict_match.group(1))
        
        # Extract quarantine
        quarantine_match = re.search(r"(\d+)\s+machine.*quarantine", output, re.IGNORECASE)
        if quarantine_match:
            parsed["quarantine_count"] = int(quarantine_match.group(1))
        
        return parsed
    
    def get_status(self) -> SyncStatus:
        """
        Get current sync status.
        
        Reads from sync repo and database to determine:
        - Current state (idle, syncing, conflict, failed, quarantine)
        - Last sync time
        - Next sync time (from daemon state)
        - Active/quarantined machines
        - Pending conflicts
        """
        # Check if currently running
        if self.is_running():
            state = "syncing"
        else:
            # Determine state from last sync
            # This will be enhanced once we integrate with history database
            state = "idle"
        
        # Placeholder - will be populated from database
        return SyncStatus(
            state=state,
            last_sync=None,
            next_sync=None,
            scope="personal",
            machines_active=0,
            machines_quarantined=0,
            conflicts_pending=0
        )
    
    def get_daemon_config(self) -> dict:
        """Get daemon configuration from database."""
        # Placeholder - will read from daemon_state table
        return {
            "enabled": True,
            "scope": "personal",
            "interval": 300
        }
    
    def update_daemon_config(self, **kwargs) -> dict:
        """Update daemon configuration."""
        # Placeholder - will write to daemon_state table
        config = self.get_daemon_config()
        config.update(kwargs)
        return config
