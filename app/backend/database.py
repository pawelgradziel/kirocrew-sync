"""
Database schema for KiroCrew Sync app.

Stores sync history, conflicts, quarantine status, and daemon state.
"""

import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Database schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    duration_ms INTEGER,
    scope TEXT NOT NULL CHECK(scope IN ('personal', 'team')),
    strategy TEXT,
    dry_run BOOLEAN DEFAULT 0,
    changes_detected BOOLEAN DEFAULT 0,
    rows_merged INTEGER DEFAULT 0,
    conflicts_count INTEGER DEFAULT 0,
    quarantine_count INTEGER DEFAULT 0,
    output TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_timestamp ON sync_runs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_sync_runs_scope ON sync_runs(scope);

CREATE TABLE IF NOT EXISTS sync_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    change_type TEXT NOT NULL CHECK(change_type IN ('knowledge', 'artifact', 'lesson', 'transcript', 'config')),
    action TEXT NOT NULL CHECK(action IN ('added', 'updated', 'deleted', 'conflict')),
    item_id TEXT,
    details TEXT,
    FOREIGN KEY (run_id) REFERENCES sync_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sync_changes_run_id ON sync_changes(run_id);

CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    machine TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_id TEXT NOT NULL,
    local_value TEXT,
    remote_value TEXT,
    resolved BOOLEAN DEFAULT 0,
    resolution TEXT CHECK(resolution IS NULL OR resolution IN ('local-wins', 'remote-wins', 'manual')),
    resolved_at TEXT,
    FOREIGN KEY (run_id) REFERENCES sync_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_conflicts_resolved ON conflicts(resolved);
CREATE INDEX IF NOT EXISTS idx_conflicts_run_id ON conflicts(run_id);

CREATE TABLE IF NOT EXISTS quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine TEXT UNIQUE NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('version_mismatch', 'embedding_mismatch', 'scope_mismatch', 'other')),
    detected_at TEXT NOT NULL,
    cleared_at TEXT,
    details TEXT
);

CREATE INDEX IF NOT EXISTS idx_quarantine_machine ON quarantine(machine);
CREATE INDEX IF NOT EXISTS idx_quarantine_active ON quarantine(cleared_at) WHERE cleared_at IS NULL;

-- Cross-process notification dedup state. NotificationService (see
-- notifications.py) suppresses a repeat push to the same (channel,
-- dedup_key) within a rolling window; keying that here rather than only in
-- an in-memory dict is what lets the long-lived FastAPI backend and the
-- short-lived, freshly-spawned cron CLI (backend/cli.py, which exits right
-- after one sync tick) agree on dedup state instead of the CLI re-notifying
-- on every single tick. sent_at is a Unix timestamp (float seconds, i.e.
-- time.time()), not an ISO string like sync_runs.timestamp, because the
-- only thing ever done with it is a "how many seconds ago" comparison.
CREATE TABLE IF NOT EXISTS notification_dedup (
    channel TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    sent_at REAL NOT NULL,
    PRIMARY KEY (channel, dedup_key)
);

-- Supports NotificationService's opportunistic prune (a DELETE WHERE
-- sent_at < cutoff run alongside every write, see notifications.py) so this
-- table stays bounded without a separate scheduled job.
CREATE INDEX IF NOT EXISTS idx_notification_dedup_sent_at ON notification_dedup(sent_at);

CREATE TABLE IF NOT EXISTS daemon_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Default daemon state
INSERT OR IGNORE INTO daemon_state (key, value) VALUES ('enabled', 'true');
INSERT OR IGNORE INTO daemon_state (key, value) VALUES ('scope', 'personal');
INSERT OR IGNORE INTO daemon_state (key, value) VALUES ('interval', '300');
INSERT OR IGNORE INTO daemon_state (key, value) VALUES ('last_run', '');
INSERT OR IGNORE INTO daemon_state (key, value) VALUES ('next_run', '');
"""


class Database:
    """Database connection manager."""
    
    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            # Default to app data directory
            db_path = Path.home() / ".kiro/crew/apps/kirocrew-sync/data/history.db"
        
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
    
    def connect(self) -> sqlite3.Connection:
        """Create database connection."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    
    def initialize(self):
        """Initialize database schema."""
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()
        logger.info("Database initialized at %s", self.db_path)
    
    def reset(self):
        """Reset database (delete and recreate)."""
        if self.db_path.exists():
            self.db_path.unlink()
        self.initialize()
        print(f"✅ Database reset at {self.db_path}")


if __name__ == "__main__":
    import sys
    
    db = Database()
    
    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        db.reset()
    else:
        db.initialize()
        print(f"✅ Database initialized at {db.db_path}")
