"""
Tests for backend/quarantine.py: add_quarantine, re-adding the same
machine, get_quarantined excluding cleared, clear_quarantine on an absent
machine, and get_quarantine_count.
"""

import pytest

from backend.quarantine import QuarantineManager


@pytest.fixture
def mgr(db_path):
    m = QuarantineManager(db_path)
    m.db.initialize()  # QuarantineManager itself doesn't auto-init the schema
    return m


def test_add_quarantine_appears_in_get_quarantined(mgr):
    mgr.add_quarantine(machine="laptop-a", reason="version_mismatch", details="v1 vs v2")
    machines = mgr.get_quarantined()
    assert len(machines) == 1
    assert machines[0].machine == "laptop-a"
    assert machines[0].reason == "version_mismatch"
    assert machines[0].details == "v1 vs v2"
    assert machines[0].cleared_at is None


def test_readding_same_machine_replaces_not_duplicates(mgr):
    mgr.add_quarantine(machine="laptop-a", reason="version_mismatch", details="first")
    mgr.add_quarantine(machine="laptop-a", reason="embedding_mismatch", details="second")

    machines = mgr.get_quarantined()
    assert len(machines) == 1
    assert machines[0].reason == "embedding_mismatch"
    assert machines[0].details == "second"
    assert mgr.get_quarantine_count() == 1


def test_get_quarantined_excludes_cleared_machines(mgr):
    mgr.add_quarantine(machine="laptop-a", reason="version_mismatch")
    mgr.add_quarantine(machine="laptop-b", reason="other")

    cleared = mgr.clear_quarantine("laptop-a")
    assert cleared is True

    machines = mgr.get_quarantined()
    assert [m.machine for m in machines] == ["laptop-b"]


def test_clear_quarantine_on_absent_machine_returns_false(mgr):
    result = mgr.clear_quarantine("never-quarantined")
    assert result is False


def test_clear_quarantine_on_already_cleared_machine_returns_false(mgr):
    """clear_quarantine's WHERE clause requires cleared_at IS NULL, so
    clearing an already-cleared machine a second time must not report
    success (and must not clobber the original cleared_at)."""
    mgr.add_quarantine(machine="laptop-a", reason="other")
    first = mgr.clear_quarantine("laptop-a")
    second = mgr.clear_quarantine("laptop-a")
    assert first is True
    assert second is False


def test_get_quarantine_count_counts_only_active(mgr):
    mgr.add_quarantine(machine="laptop-a", reason="other")
    mgr.add_quarantine(machine="laptop-b", reason="other")
    mgr.add_quarantine(machine="laptop-c", reason="other")
    mgr.clear_quarantine("laptop-b")

    assert mgr.get_quarantine_count() == 2


# ---------------------------------------------------------------------
# Idempotent re-reporting across repeated syncs (see add_quarantine's
# docstring RULE) -- ingest_artifacts calls add_quarantine once per machine
# listed in quarantine.txt on *every* sync, so these simulate that.
# ---------------------------------------------------------------------

def _raw_row(mgr, machine):
    with mgr.db.connect() as conn:
        return conn.execute(
            "SELECT * FROM quarantine WHERE machine = ?", (machine,)
        ).fetchone()


def test_readding_active_machine_preserves_id_and_detected_at(mgr):
    """A machine that stays incompatible is re-reported by ingest_artifacts
    on every sync. Its row id and detected_at must not change -- otherwise
    'Quarantined X ago' always reads 'just now' and any stable reference
    (UI deep link, notification keyed on id) breaks on the next sync."""
    first_id = mgr.add_quarantine(machine="laptop-a", reason="version_mismatch", details="run 1")
    first_row = _raw_row(mgr, "laptop-a")

    for i in range(2, 6):
        returned_id = mgr.add_quarantine(
            machine="laptop-a", reason="version_mismatch", details=f"run {i}"
        )
        row = _raw_row(mgr, "laptop-a")
        assert returned_id == first_id
        assert row["id"] == first_row["id"]
        assert row["detected_at"] == first_row["detected_at"]
        assert row["cleared_at"] is None

    # The last call's details did take effect -- it's not a dead no-op.
    assert _raw_row(mgr, "laptop-a")["details"] == "run 5"
    assert mgr.get_quarantine_count() == 1


def test_readding_cleared_machine_does_not_resurrect(mgr):
    """Once the user clears a machine, a machine that quarantine.txt keeps
    relisting (because the bash engine still finds it incompatible every
    sync) must not silently flip cleared_at back to NULL -- that would undo
    the user's Dismiss on the very next sync, which is the bug this fixes.
    """
    mgr.add_quarantine(machine="laptop-a", reason="version_mismatch")
    assert mgr.clear_quarantine("laptop-a") is True
    cleared_row = _raw_row(mgr, "laptop-a")
    assert cleared_row["cleared_at"] is not None

    # Simulate several more syncs that keep finding it incompatible.
    for _ in range(3):
        mgr.add_quarantine(machine="laptop-a", reason="version_mismatch", details="still broken")
        row = _raw_row(mgr, "laptop-a")
        assert row["id"] == cleared_row["id"]
        assert row["cleared_at"] == cleared_row["cleared_at"]  # untouched
        assert row["detected_at"] == cleared_row["detected_at"]  # untouched

    # And it correctly stays out of the active list/count.
    assert mgr.get_quarantined() == []
    assert mgr.get_quarantine_count() == 0
