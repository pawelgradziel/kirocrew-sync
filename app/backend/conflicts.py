"""
Conflict manager - handles conflict resolution.
"""

from datetime import datetime
from typing import List, Optional
from pathlib import Path

from .database import Database
from .models import Conflict


class ConflictManager:
    """Manages sync conflicts."""
    
    def __init__(self, db_path: Optional[Path] = None):
        self.db = Database(db_path)
    
    def record_conflict(
        self,
        run_id: int,
        machine: str,
        table_name: str,
        row_id: str,
        local_value: Optional[str] = None,
        remote_value: Optional[str] = None
    ) -> int:
        """Record a conflict from sync."""
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO conflicts (
                    run_id, machine, table_name, row_id, local_value, remote_value
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, machine, table_name, row_id, local_value, remote_value)
            )
            conn.commit()
            return cursor.lastrowid
    
    def get_unresolved(self) -> List[Conflict]:
        """Get all unresolved conflicts."""
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                SELECT id, run_id, machine, table_name, row_id,
                       local_value, remote_value, resolved, resolution, resolved_at
                FROM conflicts
                WHERE resolved = 0
                ORDER BY id DESC
                """
            )
            return [
                Conflict(
                    id=row['id'],
                    run_id=row['run_id'],
                    machine=row['machine'],
                    table_name=row['table_name'],
                    row_id=row['row_id'],
                    local_value=row['local_value'],
                    remote_value=row['remote_value'],
                    resolved=bool(row['resolved']),
                    resolution=row['resolution'],
                    resolved_at=datetime.fromisoformat(row['resolved_at']) if row['resolved_at'] else None
                )
                for row in cursor.fetchall()
            ]
    
    def resolve_conflict(self, conflict_id: int, resolution: str) -> bool:
        """Mark conflict as resolved."""
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE conflicts
                SET resolved = 1, resolution = ?, resolved_at = ?
                WHERE id = ?
                """,
                (resolution, datetime.now().isoformat(), conflict_id)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def get_conflict_count(self) -> int:
        """Get count of unresolved conflicts."""
        with self.db.connect() as conn:
            cursor = conn.execute("SELECT COUNT(*) as count FROM conflicts WHERE resolved = 0")
            return cursor.fetchone()['count']
