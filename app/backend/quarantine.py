"""
Quarantine manager - tracks quarantined machines.
"""

from datetime import datetime
from typing import List, Optional
from pathlib import Path

from .database import Database
from .models import QuarantinedMachine


class QuarantineManager:
    """Manages quarantined machines."""
    
    def __init__(self, db_path: Optional[Path] = None):
        self.db = Database(db_path)
    
    def add_quarantine(
        self,
        machine: str,
        reason: str,
        details: Optional[str] = None
    ) -> int:
        """Add machine to quarantine."""
        with self.db.connect() as conn:
            # Use INSERT OR REPLACE to update if machine already exists
            cursor = conn.execute(
                """
                INSERT OR REPLACE INTO quarantine (machine, reason, detected_at, details)
                VALUES (?, ?, ?, ?)
                """,
                (machine, reason, datetime.now().isoformat(), details)
            )
            conn.commit()
            return cursor.lastrowid
    
    def get_quarantined(self) -> List[QuarantinedMachine]:
        """Get all currently quarantined machines."""
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                SELECT id, machine, reason, detected_at, cleared_at, details
                FROM quarantine
                WHERE cleared_at IS NULL
                ORDER BY detected_at DESC
                """
            )
            return [
                QuarantinedMachine(
                    id=row['id'],
                    machine=row['machine'],
                    reason=row['reason'],
                    detected_at=datetime.fromisoformat(row['detected_at']),
                    cleared_at=datetime.fromisoformat(row['cleared_at']) if row['cleared_at'] else None,
                    details=row['details']
                )
                for row in cursor.fetchall()
            ]
    
    def clear_quarantine(self, machine: str) -> bool:
        """Clear machine from quarantine."""
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE quarantine
                SET cleared_at = ?
                WHERE machine = ? AND cleared_at IS NULL
                """,
                (datetime.now().isoformat(), machine)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def get_quarantine_count(self) -> int:
        """Get count of quarantined machines."""
        with self.db.connect() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) as count FROM quarantine WHERE cleared_at IS NULL"
            )
            return cursor.fetchone()['count']
