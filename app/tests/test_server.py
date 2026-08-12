"""
Tests for backend/server.py.

Covers the fixes made in response to review findings:
  1. blocking calls run off the event loop (threadpool), verified with a
     real concurrency timing measurement
  2. exit code 3 ("ok, but N machines quarantined") no longer reported as
     GET /status state == "failed"
  3. SyncTriggerRequest.strategy rejects unknown values (422) instead of
     reaching the sync engine, and artifact ingestion is skipped when the
     sync engine exited before it could have truncated
     conflicts.jsonl/quarantine.txt
  4. POST /sync response carries explicit success/exit_code fields
  5. a timed-out sync is recorded in history (with partial output) before
     the 504 is returned, instead of vanishing without a trace
  6. GET /history rejects out-of-range limit/offset (422) instead of
     silently accepting sqlite's `LIMIT -1` == unbounded
  7. unhandled exceptions return a generic 500 message, not the raw
     exception text (which can carry filesystem paths / sqlite internals)
  8. history is pruned (cleanup_old_runs) after every recorded run
  9. (comment-only fix in server.py; nothing executable to test)
  10. an ingest_artifacts() failure does not turn a successful sync into a
      500 response

SAFETY: every test goes through the `app_env` fixture below, which points
every path-resolving environment variable backend.server reads
(KIROCREW_SYNC_DIR, KIROCREW_DIR, KIROCREW_SYNC_DB) at throwaway locations
under `tmp_path`, then reloads backend.server so its module-level manager
singletons (constructed once, at import time) are rebuilt against those
paths rather than whatever was active when the module was first imported.
This suite must NEVER touch the real ~/.kiro/crew -- see conftest.py's
`_fake_home` autouse fixture for the belt-and-suspenders backstop that
applies on top of this.
"""

import asyncio
import importlib
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.server as server_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_fake_script(
    sync_dir: Path,
    stdout_text: str = "",
    exit_code: int = 0,
    stderr_text: str = "",
    sleep: float = 0,
    pre_lines=None,
) -> Path:
    """A stand-in for kirocrew-sync.sh, in the same spirit as
    test_sync_manager.py's helper of the same name, extended with an
    optional `sleep` (for the concurrency test) and `pre_lines` (raw shell
    lines run before exiting, used to plant artifact files the way a real
    cmd_sync() run would)."""
    sync_dir.mkdir(parents=True, exist_ok=True)
    script = sync_dir / "kirocrew-sync.sh"
    lines = ["#!/usr/bin/env bash"]
    for line in (pre_lines or []):
        lines.append(line)
    if sleep:
        lines.append(f"sleep {sleep}")
    if stdout_text:
        lines.append(f'echo "{stdout_text}"')
    if stderr_text:
        lines.append(f'echo "{stderr_text}" >&2')
    lines.append(f"exit {exit_code}")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)
    return script


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Point every path env var backend.server resolves at tmp_path, then
    reload the module so its module-level managers are rebuilt against
    them. Returns the resolved paths for tests that need to poke at files
    directly (e.g. planting a stale conflicts.jsonl)."""
    sync_dir = tmp_path / "sync"
    kirocrew_dir = tmp_path / "kirocrew"
    db_path = tmp_path / "data" / "history.db"
    sync_dir.mkdir(parents=True, exist_ok=True)
    kirocrew_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("KIROCREW_SYNC_DIR", str(sync_dir))
    monkeypatch.setenv("KIROCREW_DIR", str(kirocrew_dir))
    monkeypatch.setenv("KIROCREW_SYNC_DB", str(db_path))

    importlib.reload(server_module)

    return {
        "sync_dir": sync_dir,
        "kirocrew_dir": kirocrew_dir,
        "sync_root": kirocrew_dir / ".sync",
        "db_path": db_path,
    }


@pytest.fixture
def client(app_env):
    return TestClient(server_module.app)


# ---------------------------------------------------------------------------
# Finding 1: blocking work must not freeze the event loop
# ---------------------------------------------------------------------------

def test_status_stays_responsive_during_a_slow_sync(app_env):
    """A concurrent GET /status must return promptly while a slow POST
    /sync is in flight on the *same* event loop. This exercises the actual
    production shape (one process, one event loop, overlapping requests):
    prior to wrapping every blocking call in run_in_threadpool, the sync
    engine's subprocess.run() call would block the whole loop and every
    other handler with it -- verified by hand before this fix: a status
    check issued 0.5s into an 8s sync did not return for the remainder of
    the sync."""
    _write_fake_script(
        app_env["sync_dir"],
        stdout_text="Sync complete",
        exit_code=0,
        sleep=1.5,
    )

    async def scenario():
        transport = httpx.ASGITransport(app=server_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            sync_task = asyncio.create_task(ac.post("/sync", json={"strategy": "auto"}))
            await asyncio.sleep(0.4)  # let the sync actually start

            start = time.monotonic()
            status_resp = await ac.get("/status")
            status_elapsed = time.monotonic() - start

            sync_resp = await sync_task
            return status_resp, status_elapsed, sync_resp

    status_resp, status_elapsed, sync_resp = asyncio.run(scenario())

    assert status_resp.status_code == 200
    # The sync sleeps 1.5s; a responsive /status should come back in a
    # small fraction of that, not be stuck behind it.
    assert status_elapsed < 0.5, (
        f"/status took {status_elapsed:.2f}s while a sync was running -- "
        "the event loop appears to be blocked"
    )
    # And it should correctly observe the sync in progress.
    assert status_resp.json()["status"]["state"] == "syncing"

    assert sync_resp.status_code == 200
    assert sync_resp.json()["success"] is True


# ---------------------------------------------------------------------------
# Finding 2: exit code 3 is success-with-quarantine, not "failed"
# ---------------------------------------------------------------------------

def test_status_exit_code_3_is_not_reported_as_failed(app_env, client):
    from backend.models import SyncResult

    server_module.history_mgr.record_sync(SyncResult(
        exit_code=3,
        duration_ms=1000,
        scope="personal",
        strategy="auto",
        dry_run=False,
        changes_detected=True,
        rows_merged=5,
        conflicts_count=0,
        quarantine_count=1,
        output="Sync complete, with 1 machine(s) still quarantined",
        error=None,
    ))

    resp = client.get("/status")
    assert resp.status_code == 200
    state = resp.json()["status"]["state"]
    assert state != "failed"
    # No active quarantine row was inserted into the `quarantine` table
    # (only a sync_runs row), so with nothing else pending the state should
    # settle to idle.
    assert state == "idle"


def test_status_exit_code_1_is_still_reported_as_failed(app_env, client):
    """Regression guard: only 0 and 3 are success codes -- a genuine
    failure must still surface as failed."""
    from backend.models import SyncResult

    server_module.history_mgr.record_sync(SyncResult(
        exit_code=1,
        duration_ms=500,
        scope="personal",
        strategy="auto",
        dry_run=False,
        changes_detected=False,
        rows_merged=0,
        conflicts_count=0,
        quarantine_count=0,
        output="",
        error="boom",
    ))

    resp = client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["status"]["state"] == "failed"


# ---------------------------------------------------------------------------
# Finding 3: strategy validation + ingestion only when truncation happened
# ---------------------------------------------------------------------------

def test_sync_rejects_unknown_strategy_with_422(app_env, client):
    resp = client.post("/sync", json={"strategy": "bogus"})
    assert resp.status_code == 422


def test_sync_skips_ingestion_when_engine_exits_before_truncation(app_env, client):
    """Simulates kirocrew-sync.sh exiting 1 from argument parsing / require_
    python / require_git -- i.e. before cmd_sync() ever truncates
    conflicts.jsonl. A stale conflicts.jsonl left over from some earlier
    run must NOT be re-ingested as a new conflict."""
    sync_root = app_env["sync_root"]
    sync_root.mkdir(parents=True, exist_ok=True)
    (sync_root / "conflicts.jsonl").write_text(
        '{"table": "knowledge", "key": "row-1", "kind": "edit/edit", '
        '"resolution": "unresolved", "path": "knowledge/row-1.json"}\n'
    )

    _write_fake_script(
        app_env["sync_dir"],
        stderr_text="python3 is not installed",
        exit_code=1,
        # deliberately no "Unpacking local state" marker: this run never
        # reached cmd_sync's body at all.
    )

    resp = client.post("/sync", json={"strategy": "auto"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert body["success"] is False
    assert body["exit_code"] == 1

    conflicts = client.get("/conflicts").json()
    assert conflicts["total"] == 0, (
        "stale conflicts.jsonl from a prior run was re-ingested as a new "
        "conflict even though this run never reached truncation"
    )


def test_sync_ingests_when_engine_exits_after_truncation(app_env, client):
    """A run that DOES reach cmd_sync's body (logs the truncation marker)
    and then hits a real mid-merge conflict (exit 1) must still have its
    freshly-written conflicts.jsonl ingested."""
    sync_root = app_env["sync_root"]
    conflicts_path = sync_root / "conflicts.jsonl"

    _write_fake_script(
        app_env["sync_dir"],
        pre_lines=[
            f'mkdir -p "{sync_root}"',
            'echo "Unpacking local state..."',
            (
                f'echo \'{{"table": "knowledge", "key": "row-2", '
                f'"kind": "edit/edit", "resolution": "unresolved", '
                f'"path": "knowledge/row-2.json"}}\' > "{conflicts_path}"'
            ),
        ],
        stderr_text="conflict during merge",
        exit_code=1,
    )

    resp = client.post("/sync", json={"strategy": "auto"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert body["exit_code"] == 1

    conflicts = client.get("/conflicts").json()
    assert conflicts["total"] == 1
    assert conflicts["conflicts"][0]["row_id"] == "row-2"


# ---------------------------------------------------------------------------
# Finding 4: explicit success/exit_code fields on the trigger response
# ---------------------------------------------------------------------------

def test_sync_response_reports_success_and_exit_code_on_success(app_env, client):
    _write_fake_script(app_env["sync_dir"], stdout_text="Sync complete", exit_code=0)

    resp = client.post("/sync", json={"strategy": "auto"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert body["success"] is True
    assert body["exit_code"] == 0
    assert body["run_id"] is not None
    # Backward compatible: the old English-message field still works.
    assert "exit code 0" in body["message"]


def test_sync_response_reports_failure_on_exit_1(app_env, client):
    _write_fake_script(app_env["sync_dir"], stderr_text="boom", exit_code=1)

    resp = client.post("/sync", json={"strategy": "auto"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert body["success"] is False
    assert body["exit_code"] == 1


# ---------------------------------------------------------------------------
# Finding 5: a timed-out sync is recorded, not lost
# ---------------------------------------------------------------------------

def test_sync_timeout_is_recorded_in_history_before_504(app_env, client, monkeypatch):
    def _raise_timeout(**kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["kirocrew-sync.sh", "sync"],
            timeout=300,
            output=b"partial stdout before kill",
            stderr=b"partial stderr before kill",
        )

    monkeypatch.setattr(server_module.sync_mgr, "run_sync", _raise_timeout)

    resp = client.post("/sync", json={"strategy": "auto"})
    assert resp.status_code == 504
    assert "recorded as run" in resp.json()["detail"]

    history = client.get("/history").json()
    assert history["total"] == 1
    run = history["runs"][0]
    assert run["exit_code"] == -1
    assert "timed out" in run["error"]

    details = client.get(f"/history/{run['id']}").json()
    assert "partial stdout before kill" in details["output"]
    assert "partial stderr before kill" in details["output"]


# ---------------------------------------------------------------------------
# Finding 6: /history limit/offset bounds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("params", [
    {"limit": -1},
    {"limit": 0},
    {"limit": 100000},
    {"offset": -5},
])
def test_history_rejects_out_of_range_bounds(app_env, client, params):
    resp = client.get("/history", params=params)
    assert resp.status_code == 422


def test_history_accepts_in_range_bounds(app_env, client):
    resp = client.get("/history", params={"limit": 10, "offset": 0})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Finding 7: 500s don't leak internal detail
# ---------------------------------------------------------------------------

def test_internal_error_returns_generic_message(app_env, client, monkeypatch):
    def _boom(*args, **kwargs):
        raise ValueError("Invalid isoformat string: 'not-a-timestamp' at /home/pawel/.kiro/crew/data/history.db")

    monkeypatch.setattr(server_module.history_mgr, "get_recent", _boom)

    resp = client.get("/history")
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail == "Internal server error"
    assert "not-a-timestamp" not in detail
    assert "/home/pawel" not in detail


def test_deliberate_statuses_are_preserved(app_env, client):
    """503 (engine unavailable), 400 (ValueError), and 404 must still come
    through with their real, useful detail -- only the generic
    `except Exception` fallback was made generic."""
    # 503: no kirocrew-sync.sh was ever written for this app_env.
    resp = client.post("/sync", json={"strategy": "auto"})
    assert resp.status_code == 503
    assert "kirocrew-sync.sh" in resp.json()["detail"]

    # 404: no such conflict/run/machine.
    resp = client.get("/history/999999")
    assert resp.status_code == 404

    resp = client.post("/quarantine/no-such-machine/clear")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Finding 8: history is pruned after every recorded run
# ---------------------------------------------------------------------------

def test_cleanup_old_runs_is_called_after_a_sync(app_env, client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        server_module.history_mgr, "cleanup_old_runs",
        lambda *a, **k: calls.append((a, k)),
    )
    _write_fake_script(app_env["sync_dir"], stdout_text="Sync complete", exit_code=0)

    resp = client.post("/sync", json={"strategy": "auto"})
    assert resp.status_code == 200
    assert len(calls) >= 1


# ---------------------------------------------------------------------------
# Finding 10: ingestion failures don't turn a successful sync into a 500
# ---------------------------------------------------------------------------

def test_sync_survives_ingestion_failure(app_env, client, monkeypatch):
    _write_fake_script(app_env["sync_dir"], stdout_text="Sync complete", exit_code=0)

    def _boom(**kwargs):
        raise RuntimeError("disk read error")

    monkeypatch.setattr(server_module.sync_mgr, "ingest_artifacts", _boom)

    resp = client.post("/sync", json={"strategy": "auto"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["run_id"] is not None


# ---------------------------------------------------------------------------
# Misc: body-less POST /sync (as sent by StatusWidget.tsx) still works
# ---------------------------------------------------------------------------

def test_sync_without_body_uses_defaults(app_env, client):
    _write_fake_script(app_env["sync_dir"], stdout_text="Sync complete", exit_code=0)

    resp = client.post("/sync")
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert body["success"] is True


def test_sync_already_running_short_circuits(app_env, client, monkeypatch):
    monkeypatch.setattr(server_module.sync_mgr, "is_running", lambda: True)

    resp = client.post("/sync", json={"strategy": "auto"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is False
    assert body["success"] is False
    assert body["message"] == "Sync already in progress"
