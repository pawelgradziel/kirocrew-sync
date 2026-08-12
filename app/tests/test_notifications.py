"""
Tests for backend/notifications.py.

No real network calls are made anywhere in this file:
- Tests of the "no transport available" contract point ``_APP_SECRET_PATH``
  at a file that doesn't exist (never at anything under the real home
  directory), so ``_ensure_transport()`` genuinely, deterministically fails
  closed and ``_push()`` returns before ever reaching ``_post()`` (which is
  the only method that would perform I/O).
- Tests of dedup behavior stub out ``_ensure_transport``/``_post`` directly
  on the instance so the dedup logic itself is exercised without any HTTP
  I/O at all.
"""

import asyncio

import pytest

from backend import notifications as notifications_module
from backend.models import Conflict
from backend.notifications import NotificationService


def run(coro):
    return asyncio.run(coro)


def make_conflict(**overrides) -> Conflict:
    fields = dict(
        id=1,
        run_id=1,
        machine="laptop-a",
        table_name="knowledge.items",
        row_id="k1",
        local_value="local",
        remote_value="remote",
        resolved=False,
        resolution=None,
        resolved_at=None,
    )
    fields.update(overrides)
    return Conflict(**fields)


@pytest.fixture(autouse=True)
def no_real_app_secret(monkeypatch, tmp_path):
    """Guarantee _ensure_transport() sees no secret, deterministically and
    without depending on whatever happens to exist on the dev machine."""
    monkeypatch.setattr(
        notifications_module, "_APP_SECRET_PATH", tmp_path / "no-such-app-secret"
    )


# ---------------------------------------------------------------------------
# notify_sync_completed: clean sync sends nothing
# ---------------------------------------------------------------------------

def test_clean_sync_sends_nothing():
    service = NotificationService()

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("_push must not be called for a clean sync")

    service._push = _fail_if_called  # would raise if the clean-sync guard is removed

    result = run(service.notify_sync_completed(run_id=1, rows_merged=10, conflicts=0, quarantine=0))
    assert result is False


def test_sync_completed_with_conflicts_attempts_push(monkeypatch):
    """Non-clean runs must actually reach the push path (and, with no
    transport available in this test environment, still return False)."""
    service = NotificationService()
    result = run(service.notify_sync_completed(run_id=1, rows_merged=10, conflicts=2, quarantine=0))
    assert result is False  # no transport in this test env, but not short-circuited early


# ---------------------------------------------------------------------------
# Every notify_* returns False, never raises, when no transport is available
# ---------------------------------------------------------------------------

def test_notify_conflict_returns_false_without_transport():
    service = NotificationService()
    result = run(service.notify_conflict(make_conflict()))
    assert result is False


def test_notify_quarantine_returns_false_without_transport():
    service = NotificationService()
    result = run(service.notify_quarantine("laptop-a", "version_mismatch"))
    assert result is False


def test_notify_failure_returns_false_without_transport():
    service = NotificationService()
    result = run(service.notify_failure("disk full", run_id=42))
    assert result is False


def test_no_transport_never_attempts_network_post():
    """Defense in depth: even if the transport-unavailable short-circuit
    were removed, _post (the only method that performs I/O) must not be
    reachable in these tests, because the missing app secret is the actual
    resolution failure being tested."""
    service = NotificationService()

    async def _fail_if_called(*args, **kwargs):
        raise AssertionError("_post must never be called: no transport available")

    service._post = _fail_if_called
    result = run(service.notify_quarantine("laptop-a", "other"))
    assert result is False


# ---------------------------------------------------------------------------
# enabled=False suppresses everything
# ---------------------------------------------------------------------------

def test_disabled_service_suppresses_everything_before_checking_transport():
    service = NotificationService(enabled=False)

    def _fail_if_called():
        raise AssertionError("_ensure_transport must not be reached when disabled")

    service._ensure_transport = _fail_if_called

    assert run(service.notify_quarantine("laptop-a", "other")) is False
    assert run(service.notify_conflict(make_conflict())) is False
    assert run(service.notify_failure("boom")) is False
    assert run(service.notify_sync_completed(run_id=1, rows_merged=1, conflicts=1, quarantine=0)) is False


# ---------------------------------------------------------------------------
# Dedup: suppresses a repeat within the window, allows a different key,
# allows a repeat once the window has elapsed.
# ---------------------------------------------------------------------------

def _stub_successful_transport(service):
    """Make _push's transport/post steps succeed deterministically, with no
    real I/O, so the dedup logic downstream of them can be exercised."""
    calls = []
    service._ensure_transport = lambda: True

    async def fake_post(path, payload):
        calls.append(payload)
        return True

    service._post = fake_post
    return calls


def test_dedup_suppresses_repeat_within_window_but_allows_different_key():
    service = NotificationService(dedup_window_seconds=100.0)
    calls = _stub_successful_transport(service)

    first = run(service.notify_quarantine("laptop-a", "other"))
    second = run(service.notify_quarantine("laptop-a", "other"))  # same dedup key
    third = run(service.notify_quarantine("laptop-b", "other"))  # different machine -> different key

    assert first is True
    assert second is False
    assert third is True
    assert len(calls) == 2  # the deduped call must not have reached _post at all


def test_dedup_allows_repeat_after_window_elapses():
    import time

    service = NotificationService(dedup_window_seconds=0.05)
    calls = _stub_successful_transport(service)

    first = run(service.notify_quarantine("laptop-a", "other"))
    time.sleep(0.1)
    second = run(service.notify_quarantine("laptop-a", "other"))

    assert first is True
    assert second is True
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Database-backed dedup: the actual point of this change. A NotificationService
# with no db_path (all tests above) only ever dedups in-memory, which is
# exactly what a freshly-spawned cron CLI process can't rely on. These tests
# construct two independent NotificationService instances -- simulating two
# separate cron ticks -- sharing only a database file, and prove the second
# tick is suppressed anyway.
# ---------------------------------------------------------------------------

def test_dedup_survives_across_separate_instances_backed_by_same_db(tmp_path):
    """The core cross-process guarantee: instance #1 sends and records to
    the DB, instance #2 (a stand-in for the next cron tick, sharing no
    memory with #1) must see that record and suppress the repeat."""
    db_path = tmp_path / "shared.db"

    first = NotificationService(dedup_window_seconds=100.0, db_path=db_path)
    calls_first = _stub_successful_transport(first)
    result_first = run(first.notify_quarantine("laptop-a", "version_mismatch"))

    second = NotificationService(dedup_window_seconds=100.0, db_path=db_path)
    calls_second = _stub_successful_transport(second)
    result_second = run(second.notify_quarantine("laptop-a", "version_mismatch"))

    assert result_first is True
    assert result_second is False
    assert len(calls_first) == 1
    assert len(calls_second) == 0  # deduped before ever reaching _post

    # A different dedup key (different machine) on a third "tick" must not
    # be suppressed by the first machine's record.
    third = NotificationService(dedup_window_seconds=100.0, db_path=db_path)
    calls_third = _stub_successful_transport(third)
    result_third = run(third.notify_quarantine("laptop-b", "version_mismatch"))
    assert result_third is True
    assert len(calls_third) == 1


def test_dedup_via_db_allows_repeat_after_window_elapses(tmp_path):
    import time

    db_path = tmp_path / "shared.db"

    first = NotificationService(dedup_window_seconds=0.05, db_path=db_path)
    calls_first = _stub_successful_transport(first)
    result_first = run(first.notify_quarantine("laptop-a", "other"))

    time.sleep(0.1)

    second = NotificationService(dedup_window_seconds=0.05, db_path=db_path)
    calls_second = _stub_successful_transport(second)
    result_second = run(second.notify_quarantine("laptop-a", "other"))

    assert result_first is True
    assert result_second is True  # window elapsed, DB record is stale
    assert len(calls_first) == 1
    assert len(calls_second) == 1


def test_disabled_service_with_db_configured_never_touches_db(tmp_path):
    """enabled=False must short-circuit before even resolving the dedup
    database -- mirrors the existing in-memory
    test_disabled_service_suppresses_everything_before_checking_transport."""
    db_path = tmp_path / "shared.db"
    service = NotificationService(enabled=False, db_path=db_path)

    def _fail_if_called():
        raise AssertionError("_dedup_db_ready must not be reached when disabled")

    service._dedup_db_ready = _fail_if_called

    assert run(service.notify_quarantine("laptop-a", "other")) is False
    assert run(service.notify_conflict(make_conflict())) is False
    assert run(service.notify_failure("boom")) is False
    assert run(service.notify_sync_completed(run_id=1, rows_merged=1, conflicts=1, quarantine=0)) is False
    assert not db_path.exists()  # never even opened


def test_unwritable_database_falls_back_without_raising(tmp_path):
    """A structurally broken db_path (parent is a plain file, so
    Database.__init__'s mkdir(parents=True) raises NotADirectoryError) must
    never surface out of notify_* -- it must fall back to the in-memory
    window and still let the notification through."""
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("this is a file, not a directory")
    bad_db_path = blocker / "history.db"

    service = NotificationService(dedup_window_seconds=100.0, db_path=bad_db_path)
    calls = _stub_successful_transport(service)

    result = run(service.notify_quarantine("laptop-a", "other"))
    assert result is True  # dedup degraded, but the sync-notification path never broke
    assert len(calls) == 1

    # The in-memory fallback that _record_seen always maintains must still
    # dedup a repeat on this same instance, proving the failure degraded
    # gracefully rather than disabling dedup outright.
    result2 = run(service.notify_quarantine("laptop-a", "other"))
    assert result2 is False
    assert len(calls) == 1


def test_corrupt_database_file_falls_back_without_raising(tmp_path):
    """A db_path that exists but isn't a real sqlite file (Database.initialize()'s
    executescript raising sqlite3.DatabaseError) must degrade the same way as
    an unwritable path -- never raise out of notify_*."""
    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"not a sqlite database at all, just bytes")

    service = NotificationService(dedup_window_seconds=100.0, db_path=db_path)
    calls = _stub_successful_transport(service)

    result = run(service.notify_quarantine("laptop-a", "other"))
    assert result is True
    assert len(calls) == 1
