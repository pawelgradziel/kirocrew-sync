"""
Tests for backend/conflicts.py: record_conflict (including resolved/
resolution kwargs), get_unresolved excluding resolved rows, resolve_conflict
return value for a missing id, and get_conflict_count.
"""

import pytest

from backend.conflicts import ConflictManager


@pytest.fixture
def mgr(db_path):
    m = ConflictManager(db_path)
    m.db.initialize()  # ConflictManager itself doesn't auto-init the schema
    return m


@pytest.fixture
def run_id(mgr):
    """conflicts.run_id is a real foreign key (ON DELETE CASCADE) into
    sync_runs, enforced by PRAGMA foreign_keys = ON (see Database.connect).
    Every conflict recorded in these tests needs a real parent row."""
    with mgr.db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO sync_runs (timestamp, exit_code, scope, output) "
            "VALUES ('2026-01-01T00:00:00', 0, 'personal', '')"
        )
        conn.commit()
        return cursor.lastrowid


def _raw_row(mgr, conflict_id):
    with mgr.db.connect() as conn:
        return conn.execute(
            "SELECT * FROM conflicts WHERE id = ?", (conflict_id,)
        ).fetchone()


def test_record_conflict_defaults_to_unresolved(mgr, run_id):
    conflict_id = mgr.record_conflict(
        run_id=run_id, machine="m1", table_name="knowledge.items", row_id="k1"
    )
    row = _raw_row(mgr, conflict_id)
    assert row["resolved"] == 0
    assert row["resolution"] is None
    assert row["resolved_at"] is None


def test_record_conflict_with_resolved_and_resolution_kwargs(mgr, run_id):
    conflict_id = mgr.record_conflict(
        run_id=run_id,
        machine="m1",
        table_name="lessons",
        row_id="l1",
        local_value="local text",
        remote_value="remote text",
        resolved=True,
        resolution="remote-wins",
    )
    row = _raw_row(mgr, conflict_id)
    assert row["resolved"] == 1
    assert row["resolution"] == "remote-wins"
    assert row["resolved_at"] is not None
    assert row["local_value"] == "local text"
    assert row["remote_value"] == "remote text"


def test_record_conflict_resolved_false_has_no_resolved_at_even_with_resolution(mgr, run_id):
    """resolved=False must not set resolved_at, regardless of what
    resolution is passed -- resolved_at only reflects actual resolution."""
    conflict_id = mgr.record_conflict(
        run_id=run_id, machine="m1", table_name="t", row_id="r1", resolved=False
    )
    row = _raw_row(mgr, conflict_id)
    assert row["resolved_at"] is None


def test_get_unresolved_excludes_resolved_rows(mgr, run_id):
    unresolved_id = mgr.record_conflict(run_id=run_id, machine="m1", table_name="t", row_id="a")
    resolved_id = mgr.record_conflict(
        run_id=run_id, machine="m1", table_name="t", row_id="b",
        resolved=True, resolution="local-wins",
    )

    unresolved = mgr.get_unresolved()
    ids = [c.id for c in unresolved]
    assert unresolved_id in ids
    assert resolved_id not in ids
    assert len(unresolved) == 1


def test_get_unresolved_orders_newest_first(mgr, run_id):
    first = mgr.record_conflict(run_id=run_id, machine="m1", table_name="t", row_id="a")
    second = mgr.record_conflict(run_id=run_id, machine="m1", table_name="t", row_id="b")
    unresolved = mgr.get_unresolved()
    assert [c.id for c in unresolved] == [second, first]


def test_resolve_conflict_updates_row_and_returns_true(mgr, run_id):
    conflict_id = mgr.record_conflict(run_id=run_id, machine="m1", table_name="t", row_id="a")
    result = mgr.resolve_conflict(conflict_id, "local-wins")
    assert result is True

    row = _raw_row(mgr, conflict_id)
    assert row["resolved"] == 1
    assert row["resolution"] == "local-wins"
    assert row["resolved_at"] is not None

    # And it must actually leave get_unresolved()
    assert conflict_id not in [c.id for c in mgr.get_unresolved()]


def test_resolve_conflict_missing_id_returns_false(mgr, run_id):
    result = mgr.resolve_conflict(999999, "local-wins")
    assert result is False


def test_resolving_already_resolved_conflict_returns_false_and_preserves_original(mgr, run_id):
    """The UPDATE only touches rows still at resolved = 0, so resolving the
    same conflict twice must not report success the second time, and must
    not clobber the first resolution/resolved_at with the second one."""
    conflict_id = mgr.record_conflict(run_id=run_id, machine="m1", table_name="t", row_id="a")

    first = mgr.resolve_conflict(conflict_id, "local-wins")
    first_row = _raw_row(mgr, conflict_id)
    assert first is True
    assert first_row["resolution"] == "local-wins"

    second = mgr.resolve_conflict(conflict_id, "remote-wins")
    second_row = _raw_row(mgr, conflict_id)
    assert second is False
    assert second_row["resolution"] == "local-wins"  # unchanged, not "remote-wins"
    assert second_row["resolved_at"] == first_row["resolved_at"]  # unchanged


def test_get_conflict_count_counts_only_unresolved(mgr, run_id):
    mgr.record_conflict(run_id=run_id, machine="m1", table_name="t", row_id="a")
    mgr.record_conflict(run_id=run_id, machine="m1", table_name="t", row_id="b")
    mgr.record_conflict(
        run_id=run_id, machine="m1", table_name="t", row_id="c",
        resolved=True, resolution="manual",
    )

    assert mgr.get_conflict_count() == 2

    third_id = mgr.record_conflict(run_id=run_id, machine="m1", table_name="t", row_id="d")
    mgr.resolve_conflict(third_id, "remote-wins")
    assert mgr.get_conflict_count() == 2  # unchanged: one added unresolved, then immediately resolved
