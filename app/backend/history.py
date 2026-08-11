"""
History manager - tracks sync runs and changes.
"""

from datetime import datetime
from typing import List, Optional
from pathlib import Path

from .database import Database
from .models import SyncResult, SyncRun, SyncRunDetails, SyncChange


class HistoryManager:
    """Manages sync history in database."""
    
    def __init__(self, db_path: Optional[Path] = None):
        self.db = Database(db_path)
        self.db.initialize()  # Ensure schema exists
    
    def record_sync(self, result: SyncResult, changes: Optional[List[SyncChange]] = None) -> int:
        """
        Record a sync run in history.
        
        Args:
            result: SyncResult from sync_manager
            changes: Optional list of changes detected
        
        Returns:
            ID of created sync run
        """
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO sync_runs (
                    timestamp, exit_code, duration_ms, scope, strategy,
                    dry_run, changes_detected, rows_merged, conflicts_count,
                    quarantine_count, output, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(),
                    result.exit_code,
                    result.duration_ms,
                    result.scope,
                    result.strategy,
                    result.dry_run,
                    result.changes_detected,
                    result.rows_merged,
                    result.conflicts_count,
                    result.quarantine_count,
                    result.output,
                    result.error
                )
            )
            run_id = cursor.lastrowid
            
            # Record changes if provided
            if changes:
                for change in changes:
                    conn.execute(
                        """
                        INSERT INTO sync_changes (
                            run_id, change_type, action, item_id, details
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            change.change_type,
                            change.action,
                            change.item_id,
                            change.details
                        )
                    )
            
            conn.commit()
            return run_id
    
    def get_recent(self, limit: int = 50, offset: int = 0, scope: Optional[str] = None) -> List[SyncRun]:
        """
        Get recent sync runs.
        
        Args:
            limit: Maximum number of runs to return
            offset: Offset for pagination
            scope: Optional scope filter ('personal' or 'team')
        
        Returns:
            List of SyncRun objects
        """
        query = """
            SELECT 
                id, timestamp, exit_code, duration_ms, scope, strategy,
                dry_run, changes_detected, rows_merged, conflicts_count,
                quarantine_count, output, error
            FROM sync_runs
        """
        params = []
        
        if scope:
            query += " WHERE scope = ?"
            params.append(scope)
        
        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        with self.db.connect() as conn:
            cursor = conn.execute(query, params)
            runs = []
            for row in cursor.fetchall():
                runs.append(SyncRun(
                    id=row['id'],
                    timestamp=datetime.fromisoformat(row['timestamp']),
                    exit_code=row['exit_code'],
                    duration_ms=row['duration_ms'],
                    scope=row['scope'],
                    strategy=row['strategy'],
                    dry_run=bool(row['dry_run']),
                    changes_detected=bool(row['changes_detected']),
                    rows_merged=row['rows_merged'],
                    conflicts_count=row['conflicts_count'],
                    quarantine_count=row['quarantine_count'],
                    output=row['output'],
                    error=row['error']
                ))
            return runs
    
    def get_run_details(self, run_id: int) -> Optional[SyncRunDetails]:
        """
        Get detailed information about a sync run including changes.
        
        Args:
            run_id: ID of sync run
        
        Returns:
            SyncRunDetails with changes, or None if not found
        """
        with self.db.connect() as conn:
            # Get run
            cursor = conn.execute(
                """
                SELECT 
                    id, timestamp, exit_code, duration_ms, scope, strategy,
                    dry_run, changes_detected, rows_merged, conflicts_count,
                    quarantine_count, output, error
                FROM sync_runs
                WHERE id = ?
                """,
                (run_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            
            # Get changes
            changes_cursor = conn.execute(
                """
                SELECT change_type, action, item_id, details
                FROM sync_changes
                WHERE run_id = ?
                ORDER BY id
                """,
                (run_id,)
            )
            changes = [
                SyncChange(
                    change_type=change_row['change_type'],
                    action=change_row['action'],
                    item_id=change_row['item_id'],
                    details=change_row['details']
                )
                for change_row in changes_cursor.fetchall()
            ]
            
            return SyncRunDetails(
                id=row['id'],
                timestamp=datetime.fromisoformat(row['timestamp']),
                exit_code=row['exit_code'],
                duration_ms=row['duration_ms'],
                scope=row['scope'],
                strategy=row['strategy'],
                dry_run=bool(row['dry_run']),
                changes_detected=bool(row['changes_detected']),
                rows_merged=row['rows_merged'],
                conflicts_count=row['conflicts_count'],
                quarantine_count=row['quarantine_count'],
                output=row['output'],
                error=row['error'],
                changes=changes
            )
    
    def get_total_count(self, scope: Optional[str] = None) -> int:
        """Get total number of sync runs."""
        query = "SELECT COUNT(*) as count FROM sync_runs"
        params = []
        
        if scope:
            query += " WHERE scope = ?"
            params.append(scope)
        
        with self.db.connect() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchone()['count']
    
    def get_last_sync(self, scope: Optional[str] = None) -> Optional[SyncRun]:
        """Get the most recent sync run."""
        runs = self.get_recent(limit=1, scope=scope)
        return runs[0] if runs else None
    
    def cleanup_old_runs(self, keep_count: int = 100):
        """
        Clean up old sync runs, keeping only the most recent N.
        
        Args:
            keep_count: Number of runs to keep
        """
        with self.db.connect() as conn:
            conn.execute(
                """
                DELETE FROM sync_runs
                WHERE id NOT IN (
                    SELECT id FROM sync_runs
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
                """,
                (keep_count,)
            )
            conn.commit()
