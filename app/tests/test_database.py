"""
Tests for backend/database.py: schema creation, seeded daemon_state
defaults, idempotent re-initialize, and reset().
"""

import logging
from pathlib import Path

import pytest

from backend.database import Database

EXPECTED_TABLES = {
    "sync_runs",
    "sync_changes",
    "conflicts",
    "quarantine",
    "daemon_state",
    "notification_dedup",
}

DEFAULT_DAEMON_STATE = {
    "enabled": "true",
    "scope": "personal",
    "interval": "300",
    "last_run": "",
    "next_run": "",
}


def _tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row["name"] for row in rows}


def _daemon_state(conn):
    rows = conn.execute("SELECT key, value FROM daemon_state").fetchall()
    return {row["key"]: row["value"] for row in rows}


def test_initialize_creates_all_expected_tables(db_path):
    db = Database(db_path)
    db.initialize()
    with db.connect() as conn:
        tables = _tables(conn)
    assert EXPECTED_TABLES <= tables


def test_initialize_creates_db_file_at_given_path(db_path):
    assert not db_path.exists()
    db = Database(db_path)
    db.initialize()
    assert db_path.exists()
    assert db_path.is_file()


def test_daemon_state_seeded_with_documented_defaults(db_path):
    db = Database(db_path)
    db.initialize()
    with db.connect() as conn:
        state = _daemon_state(conn)
    assert state == DEFAULT_DAEMON_STATE


def test_connect_enables_foreign_keys_pragma(db_path):
    db = Database(db_path)
    db.initialize()
    with db.connect() as conn:
        (fk_on,) = conn.execute("PRAGMA foreign_keys").fetchone()
    assert fk_on == 1


def test_reinitialize_is_idempotent_and_preserves_existing_data(db_path):
    """Re-running initialize() must not wipe or duplicate existing rows --
    CREATE TABLE IF NOT EXISTS / INSERT OR IGNORE must actually behave that
    way against a live database, not just look that way in the schema text."""
    db = Database(db_path)
    db.initialize()

    with db.connect() as conn:
        conn.execute(
            "UPDATE daemon_state SET value = 'false' WHERE key = 'enabled'"
        )
        conn.execute(
            "INSERT INTO sync_runs (timestamp, exit_code, scope, output) "
            "VALUES ('2026-01-01T00:00:00', 0, 'personal', 'hello')"
        )
        conn.commit()

    # Re-initialize against the same file.
    db.initialize()

    with db.connect() as conn:
        tables = _tables(conn)
        state = _daemon_state(conn)
        run_count = conn.execute("SELECT COUNT(*) AS c FROM sync_runs").fetchone()["c"]

    assert EXPECTED_TABLES <= tables
    # INSERT OR IGNORE must not have clobbered our override back to 'true'.
    assert state["enabled"] == "false"
    # The pre-existing row must survive re-initialization untouched/undup'd.
    assert run_count == 1


def test_reset_deletes_and_recreates_with_fresh_defaults(db_path):
    db = Database(db_path)
    db.initialize()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO sync_runs (timestamp, exit_code, scope, output) "
            "VALUES ('2026-01-01T00:00:00', 0, 'personal', 'hello')"
        )
        conn.execute("UPDATE daemon_state SET value = 'false' WHERE key = 'enabled'")
        conn.commit()

    db.reset()

    assert db_path.exists()
    with db.connect() as conn:
        run_count = conn.execute("SELECT COUNT(*) AS c FROM sync_runs").fetchone()["c"]
        state = _daemon_state(conn)
    assert run_count == 0
    assert state == DEFAULT_DAEMON_STATE


def test_reset_on_nonexistent_db_does_not_raise(db_path):
    db = Database(db_path)
    assert not db_path.exists()
    db.reset()  # unlink() is guarded by exists() check
    assert db_path.exists()


def test_explicit_db_path_is_used_verbatim_and_ignores_home(db_path, monkeypatch, tmp_path):
    """Constructing Database with an explicit path must never fall back to
    (or touch) Path.home(), even if home resolves somewhere unexpected."""
    decoy_home = tmp_path / "decoy_home_should_stay_empty"
    decoy_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: decoy_home)

    db = Database(db_path)
    db.initialize()

    assert db_path.exists()
    assert list(decoy_home.rglob("*")) == []


def test_notification_dedup_table_has_expected_columns(db_path):
    db = Database(db_path)
    db.initialize()
    with db.connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(notification_dedup)")}
    assert cols == {"channel", "dedup_key", "sent_at"}


def test_notification_dedup_primary_key_rejects_duplicate_channel_and_key(db_path):
    """(channel, dedup_key) is the primary key NotificationService keys its
    dedup lookups on -- a plain INSERT of the same pair twice must fail so
    that only the upsert form (ON CONFLICT DO UPDATE) that notifications.py
    actually uses can overwrite it."""
    db = Database(db_path)
    db.initialize()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO notification_dedup (channel, dedup_key, sent_at) VALUES (?, ?, ?)",
            ("quarantine", "quarantine:laptop-a", 1000.0),
        )
        conn.commit()
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO notification_dedup (channel, dedup_key, sent_at) VALUES (?, ?, ?)",
                ("quarantine", "quarantine:laptop-a", 2000.0),
            )


def test_notification_dedup_has_sent_at_index(db_path):
    """The prune query in notifications.py filters on sent_at; make sure the
    documented index actually exists rather than just being aspirational."""
    db = Database(db_path)
    db.initialize()
    with db.connect() as conn:
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='notification_dedup'"
            )
        }
    assert "idx_notification_dedup_sent_at" in indexes


def test_initialize_logs_instead_of_printing(db_path, capsys, caplog):
    """Regression test for the stdout-pollution bug: initialize() must not
    print to stdout (it fires on every server import), but should still
    surface a human-readable message via logging."""
    db = Database(db_path)
    with caplog.at_level(logging.INFO, logger="backend.database"):
        db.initialize()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert any("initialized" in record.message.lower() for record in caplog.records)
