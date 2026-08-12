"""
Tests for backend/history.py: record_sync, pagination, scope filtering,
get_run_details (incl. changes), get_total_count, get_last_sync, and
cleanup_old_runs retention.
"""

import time

import pytest

from backend.history import HistoryManager
from backend.models import SyncChange, SyncResult


def make_result(**overrides) -> SyncResult:
    fields = dict(
        exit_code=0,
        duration_ms=1000,
        scope="personal",
        strategy="auto",
        dry_run=False,
        changes_detected=True,
        rows_merged=5,
        conflicts_count=0,
        quarantine_count=0,
        output="ok",
        error=None,
    )
    fields.update(overrides)
    return SyncResult(**fields)


@pytest.fixture
def mgr(db_path):
    return HistoryManager(db_path)


def test_record_sync_returns_incrementing_run_id(mgr):
    id1 = mgr.record_sync(make_result())
    id2 = mgr.record_sync(make_result())
    assert isinstance(id1, int)
    assert id2 > id1


def test_record_sync_persists_all_fields(mgr):
    result = make_result(
        exit_code=3,
        duration_ms=4242,
        scope="team",
        strategy="remote-wins",
        dry_run=True,
        changes_detected=True,
        rows_merged=17,
        conflicts_count=2,
        quarantine_count=1,
        output="some output",
        error="some error",
    )
    run_id = mgr.record_sync(result)
    details = mgr.get_run_details(run_id)
    assert details.exit_code == 3
    assert details.duration_ms == 4242
    assert details.scope == "team"
    assert details.strategy == "remote-wins"
    assert details.dry_run is True
    assert details.changes_detected is True
    assert details.rows_merged == 17
    assert details.conflicts_count == 2
    assert details.quarantine_count == 1
    assert details.output == "some output"
    assert details.error == "some error"


def test_record_sync_with_changes_recorded_and_retrievable(mgr):
    changes = [
        SyncChange(change_type="knowledge", action="added", item_id="k1", details="new knowledge"),
        SyncChange(change_type="lesson", action="updated", item_id="l1", details="edited lesson"),
    ]
    run_id = mgr.record_sync(make_result(), changes=changes)
    details = mgr.get_run_details(run_id)
    assert len(details.changes) == 2
    assert details.changes[0].change_type == "knowledge"
    assert details.changes[0].action == "added"
    assert details.changes[0].item_id == "k1"
    assert details.changes[1].change_type == "lesson"
    assert details.changes[1].action == "updated"


def test_get_run_details_missing_id_returns_none(mgr):
    assert mgr.get_run_details(999999) is None


def test_get_run_details_has_empty_changes_when_none_recorded(mgr):
    run_id = mgr.record_sync(make_result())
    details = mgr.get_run_details(run_id)
    assert details.changes == []


def _record_n(mgr, n, scope="personal"):
    """Record n runs with strictly increasing timestamps (insertion order),
    returning the run ids in insertion order."""
    ids = []
    for i in range(n):
        ids.append(mgr.record_sync(make_result(scope=scope, rows_merged=i)))
        time.sleep(0.002)  # guarantee distinct datetime.now().isoformat() values
    return ids


def test_get_recent_pagination_limit_and_offset(mgr):
    ids = _record_n(mgr, 5)

    page1 = mgr.get_recent(limit=2, offset=0)
    page2 = mgr.get_recent(limit=2, offset=2)
    assert [r.id for r in page1] == [ids[4], ids[3]]
    assert [r.id for r in page2] == [ids[2], ids[1]]


def test_get_recent_orders_newest_first(mgr):
    ids = _record_n(mgr, 3)
    runs = mgr.get_recent(limit=10)
    assert [r.id for r in runs] == list(reversed(ids))


def test_get_recent_scope_filtering(mgr):
    personal_ids = _record_n(mgr, 2, scope="personal")
    team_ids = _record_n(mgr, 3, scope="team")

    personal_runs = mgr.get_recent(limit=10, scope="personal")
    team_runs = mgr.get_recent(limit=10, scope="team")

    assert {r.id for r in personal_runs} == set(personal_ids)
    assert {r.id for r in team_runs} == set(team_ids)
    assert all(r.scope == "personal" for r in personal_runs)
    assert all(r.scope == "team" for r in team_runs)


def test_get_total_count_overall_and_scoped(mgr):
    _record_n(mgr, 2, scope="personal")
    _record_n(mgr, 3, scope="team")

    assert mgr.get_total_count() == 5
    assert mgr.get_total_count(scope="personal") == 2
    assert mgr.get_total_count(scope="team") == 3


def test_get_last_sync_returns_most_recent(mgr):
    ids = _record_n(mgr, 3)
    last = mgr.get_last_sync()
    assert last.id == ids[-1]


def test_get_last_sync_scoped(mgr):
    _record_n(mgr, 2, scope="personal")
    team_ids = _record_n(mgr, 2, scope="team")
    last_team = mgr.get_last_sync(scope="team")
    assert last_team.id == team_ids[-1]


def test_get_last_sync_returns_none_when_empty(mgr):
    assert mgr.get_last_sync() is None


def test_cleanup_old_runs_keeps_only_most_recent_n(mgr):
    ids = _record_n(mgr, 5)
    mgr.cleanup_old_runs(keep_count=2)

    assert mgr.get_total_count() == 2
    remaining = {r.id for r in mgr.get_recent(limit=10)}
    assert remaining == {ids[-2], ids[-1]}


def test_cleanup_old_runs_noop_when_under_limit(mgr):
    ids = _record_n(mgr, 2)
    mgr.cleanup_old_runs(keep_count=100)
    assert mgr.get_total_count() == 2
    assert {r.id for r in mgr.get_recent(limit=10)} == set(ids)
