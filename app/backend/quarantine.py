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
        # Ensure schema exists -- see the same note in ConflictManager. This
        # manager previously depended on HistoryManager being constructed
        # first in server.py; reordering those two lines broke /quarantine.
        self.db.initialize()

    def add_quarantine(
        self,
        machine: str,
        reason: str,
        details: Optional[str] = None
    ) -> int:
        """
        Record `machine` as quarantined, or acknowledge that it still is.

        The bash engine has no memory of quarantine state between runs:
        every sync truncates quarantine.txt and rebuilds it from scratch
        purely from that run's live compatibility checks (see
        kirocrew-sync.sh's cmd_sync -- `: > "$QUARANTINE_LOG"` -- and
        merge_remote_refs, which re-appends only currently-incompatible
        machines). sync_manager.ingest_artifacts calls this once per machine
        listed in that file on *every* sync, so a machine that stays
        incompatible is reported here again on every single run, with no
        way to tell "still the same unresolved problem" apart from "this
        machine left quarantine and has now genuinely re-entered it" --
        quarantine.txt carries only today's snapshot, never history.

        Given that, this method is idempotent along two axes:

        - Already active (cleared_at IS NULL): re-reporting updates
          `reason`/`details` in place but preserves `id` and `detected_at`.
          The previous `INSERT OR REPLACE` deleted and recreated the row on
          every sync, so "Quarantined X ago" perpetually read "just now"
          and any stable reference (a UI deep link, a notification keyed on
          id) broke on the very next sync.

        - Already cleared (cleared_at IS NOT NULL): re-reporting is a
          no-op -- `cleared_at` is left alone and the row is not
          reactivated.

          RULE (the judgment call): a user's "Dismiss" is honored until
          something more specific than "this machine's name is in
          quarantine.txt again" says otherwise. A bare re-appearance is NOT
          by itself treated as proof the machine "left and came back",
          because that is indistinguishable here from "never actually
          left" -- which is the overwhelmingly common case (a machine stuck
          on an old KiroCrew version relists itself every sync until it is
          upgraded, forever). Treating every relisting as a fresh episode
          would silently undo the user's Dismiss on the very next sync --
          exactly the bug this method fixes.

          Honest consequence for the UI: a machine that truly does recover
          and later regresses will also not automatically reappear in
          get_quarantined()'s active list from this signal alone, since
          quarantine.txt alone cannot distinguish that case either. That
          event is not entirely silent, though -- sync_manager's own
          new-quarantine notification path compares the freshly-read
          machine list against currently-*active* records (independent of
          this table's cleared_at), so it still flags the machine as
          notification-worthy even while this table keeps the record
          cleared. Reopening the *list* itself is left to the user
          re-clearing scrutiny some other way (e.g. quarantine detail /
          history, should the UI grow one) rather than to this method
          guessing.

        A machine with no existing row at all is inserted fresh, as before.
        """
        now = datetime.now().isoformat()
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT id, cleared_at FROM quarantine WHERE machine = ?",
                (machine,)
            ).fetchone()

            if existing is None:
                cursor = conn.execute(
                    """
                    INSERT INTO quarantine (machine, reason, detected_at, details)
                    VALUES (?, ?, ?, ?)
                    """,
                    (machine, reason, now, details)
                )
                conn.commit()
                return cursor.lastrowid

            if existing["cleared_at"] is not None:
                # Cleared: honor the dismissal. See RULE above.
                return existing["id"]

            conn.execute(
                """
                UPDATE quarantine
                SET reason = ?, details = ?
                WHERE id = ?
                """,
                (reason, details, existing["id"])
            )
            conn.commit()
            return existing["id"]
    
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
