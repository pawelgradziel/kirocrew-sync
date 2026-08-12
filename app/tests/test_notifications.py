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
