"""
Tests for the cron entry point (backend/cli.py).

The exit-code contract here is the whole point of the file: KiroCrew records
a failed cron run on a non-zero exit and auto-pauses the job after five
consecutive failures. Engine exit code 3 means "sync completed, but one or
more machines stayed quarantined" -- a success. If the CLI ever reported 3
as failure, a perfectly healthy daemon would pause itself after five ticks,
and the symptom (sync silently stops running) would look nothing like the
cause.
"""

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from backend import cli, sync_runner
from backend.models import SyncResult


def _outcome(exit_code: int) -> sync_runner.SyncOutcome:
    return sync_runner.SyncOutcome(
        started=True,
        result=SyncResult(
            exit_code=exit_code,
            duration_ms=10,
            scope="personal",
            strategy="auto",
            output="",
        ),
        run_id=1,
    )


@pytest.fixture
def stub_runner(monkeypatch):
    """Replace the shared runner so these tests exercise cli.main()'s own
    decision-making, not the engine or sqlite."""
    def _install(outcome=None, raises=None):
        def _run(*a, **k):
            if raises is not None:
                raise raises
            return outcome
        monkeypatch.setattr(sync_runner, "run_sync_and_record", _run)
        async def _notify(_o):
            return None
        monkeypatch.setattr(sync_runner, "send_sync_notifications", _notify)
    return _install


@pytest.mark.parametrize(
    "engine_exit,expected",
    [
        (0, 0),   # clean sync
        (3, 0),   # completed with quarantine -- MUST be success, see module docstring
        (1, 1),   # genuine failure
        (-1, 1),  # synthetic timeout result
    ],
)
def test_exit_code_contract(stub_runner, engine_exit, expected):
    stub_runner(outcome=_outcome(engine_exit))
    assert cli.main([]) == expected


def test_already_running_is_not_a_failure(stub_runner):
    """A tick that overlaps an in-flight sync must not count as a failure --
    five overlapping ticks would otherwise auto-pause a healthy daemon."""
    stub_runner(outcome=sync_runner.SyncOutcome(started=False, already_running=True))
    assert cli.main([]) == 0


def test_timeout_is_a_failure(stub_runner):
    stub_runner(
        outcome=sync_runner.SyncOutcome(
            started=True, timed_out=True, result=_outcome(-1).result,
            run_id=7, timeout_seconds=300,
        )
    )
    assert cli.main([]) == 1


def test_missing_engine_exits_non_zero(stub_runner):
    from backend.sync_manager import SyncEngineUnavailable
    stub_runner(raises=SyncEngineUnavailable("no engine here"))
    assert cli.main([]) == 1


def test_unexpected_error_exits_non_zero_rather_than_traceback(stub_runner):
    """A crash must become a failed cron run, not an unhandled traceback --
    cron reads the exit code, not stderr."""
    stub_runner(raises=RuntimeError("boom"))
    assert cli.main([]) == 1


def test_notification_failure_does_not_change_the_exit_code(monkeypatch, stub_runner):
    """The sync already happened and was recorded; a broken notification
    channel must not turn a successful sync into a failed cron run."""
    stub_runner(outcome=_outcome(0))
    async def _boom(_o):
        raise RuntimeError("notification transport down")
    monkeypatch.setattr(sync_runner, "send_sync_notifications", _boom)
    assert cli.main([]) == 0


def test_bad_strategy_is_rejected_by_argparse():
    """Mirrors the API's Literal-validated strategy: a typo must not reach
    the engine (where it exits 1 before truncating the artifact logs, which
    previously caused stale conflicts to be re-ingested as new ones)."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["--strategy", "bogus"])
    assert exc.value.code != 0


def test_sys_path_bootstrap_makes_backend_importable():
    """cli.py runs as a plain script from the gateway's cwd, so it must put
    its own app dir on sys.path -- relative imports would fail outright."""
    assert str(cli._APP_DIR) in sys.path
    assert (cli._APP_DIR / "backend" / "sync_runner.py").is_file()


# ---------------------------------------------------------------------------
# End-to-end: the dashboard actually gets populated
# ---------------------------------------------------------------------------
#
# The tests above stub sync_runner out to isolate cli.main()'s own decisions.
# These ones deliberately do not: the entire reason this module exists is that
# a background tick used to run the sync for real and record *nothing*, so at
# least one test has to drive the whole path against a real sqlite database
# and assert the rows a cron tick is supposed to leave behind. A regression
# here is invisible to every stubbed test in this file.
#
# SAFETY: `cli_env` points KIROCREW_SYNC_DIR/KIROCREW_DIR/KIROCREW_SYNC_DB
# under tmp_path, on top of conftest.py's `_fake_home` and
# `_no_real_notifications` autouse fixtures. The subprocess tests additionally
# set HOME in the child's environment -- see the comment there.

def _write_fake_script(sync_dir, stdout_text="", exit_code=0, sleep=0, pre_lines=None):
    """A stand-in for kirocrew-sync.sh, matching the helper of the same name
    in test_server.py / test_sync_manager.py. `pre_lines` plants artifact
    files the way a real cmd_sync() run would."""
    sync_dir.mkdir(parents=True, exist_ok=True)
    script = sync_dir / "kirocrew-sync.sh"
    lines = ["#!/usr/bin/env bash"]
    lines.extend(pre_lines or [])
    if sleep:
        lines.append(f"sleep {sleep}")
    if stdout_text:
        lines.append(f'echo "{stdout_text}"')
    lines.append(f"exit {exit_code}")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)
    return script


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Point every path env var cli.py resolves at tmp_path.

    No module reload is needed (unlike test_server.py's `app_env`): cli.py
    resolves the database and constructs its managers inside main(), per
    invocation, precisely because it runs as a fresh short-lived process
    rather than a long-lived server.
    """
    sync_dir = tmp_path / "sync"
    kirocrew_dir = tmp_path / "kirocrew"
    db_path = tmp_path / "data" / "history.db"
    sync_dir.mkdir(parents=True, exist_ok=True)
    kirocrew_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("KIROCREW_SYNC_DIR", str(sync_dir))
    monkeypatch.setenv("KIROCREW_DIR", str(kirocrew_dir))
    monkeypatch.setenv("KIROCREW_SYNC_DB", str(db_path))

    return {
        "sync_dir": sync_dir,
        "kirocrew_dir": kirocrew_dir,
        "sync_root": kirocrew_dir / ".sync",
        "db_path": db_path,
    }


def _rows(db_path, query):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(query).fetchall()]
    finally:
        conn.close()


class _RecordingService:
    """Stands in for NotificationService, recording what would be sent."""

    def __init__(self):
        self.calls = []

    async def notify_failure(self, error, run_id=None):
        self.calls.append(("failure", error, run_id))
        return True

    async def notify_quarantine(self, machine, reason):
        self.calls.append(("quarantine", machine, reason))
        return True

    async def notify_conflict(self, conflict):
        self.calls.append(("conflict", conflict.table_name, conflict.row_id))
        return True

    async def notify_sync_completed(self, run_id, rows_merged, conflicts, quarantine):
        self.calls.append(("completed", run_id, rows_merged, conflicts, quarantine))
        return True


@pytest.fixture
def notifications(monkeypatch):
    """Capture notifications at sync_runner's own seam, so nothing can reach
    the real transport even in principle (conftest already blocks it)."""
    service = _RecordingService()
    monkeypatch.setattr(sync_runner, "get_notification_service", lambda: service)
    return service


def test_a_run_populates_history_conflicts_quarantine_and_daemon_state(
    cli_env, notifications
):
    """One invocation must leave behind everything the dashboard reads. This
    is the gap the module was written to close: before it, a cron tick synced
    for real and none of these rows appeared."""
    sync_root = cli_env["sync_root"]
    conflict_line = json.dumps({
        "table": "knowledge", "key": "row-1", "kind": "update",
        "resolution": "unresolved", "path": "notes/a.md",
    })
    _write_fake_script(
        cli_env["sync_dir"],
        stdout_text="Recorded local changes",
        exit_code=0,
        pre_lines=[
            f'mkdir -p "{sync_root}"',
            f"cat > '{sync_root}/conflicts.jsonl' <<'EOF'\n{conflict_line}\nEOF",
            f"printf 'laptop-2\\n' > '{sync_root}/quarantine.txt'",
        ],
    )

    assert cli.main([]) == 0

    db = cli_env["db_path"]

    runs = _rows(db, "SELECT * FROM sync_runs")
    assert len(runs) == 1
    assert runs[0]["exit_code"] == 0
    assert runs[0]["scope"] == "personal"
    assert runs[0]["changes_detected"] == 1

    conflicts = _rows(db, "SELECT * FROM conflicts")
    assert len(conflicts) == 1
    assert conflicts[0]["table_name"] == "knowledge"
    assert conflicts[0]["row_id"] == "row-1"
    assert conflicts[0]["resolved"] == 0
    assert conflicts[0]["run_id"] == runs[0]["id"]

    quarantine = _rows(db, "SELECT * FROM quarantine WHERE cleared_at IS NULL")
    assert [q["machine"] for q in quarantine] == ["laptop-2"]

    # daemon_state.last_run is what GET /status surfaces as "last sync".
    state = {r["key"]: r["value"] for r in _rows(db, "SELECT key, value FROM daemon_state")}
    assert state.get("last_run")

    kinds = [c[0] for c in notifications.calls]
    assert "quarantine" in kinds and "conflict" in kinds and "completed" in kinds
    assert "failure" not in kinds


def test_a_real_failure_is_recorded_and_notified(cli_env, notifications):
    _write_fake_script(
        cli_env["sync_dir"],
        stdout_text="Unpacking local state\n✗ Pack failed; KiroCrew data was left unchanged.",
        exit_code=1,
    )

    assert cli.main([]) == 1

    runs = _rows(cli_env["db_path"], "SELECT * FROM sync_runs")
    assert len(runs) == 1
    assert runs[0]["exit_code"] == 1
    assert "Pack failed" in runs[0]["error"]

    failures = [c for c in notifications.calls if c[0] == "failure"]
    assert len(failures) == 1
    assert "Pack failed" in failures[0][1]


def test_missing_engine_records_no_history_row(cli_env):
    """Nothing ran, so nothing is recorded -- matching POST /api/sync's 503,
    which also leaves history untouched. Recording it would add a fresh
    "failed" run to the timeline every 5 minutes for one persistent
    misconfiguration."""
    assert not (cli_env["sync_dir"] / "kirocrew-sync.sh").exists()

    assert cli.main([]) == 1
    assert _rows(cli_env["db_path"], "SELECT * FROM sync_runs") == []


def test_a_timed_out_run_still_becomes_a_visible_history_row(cli_env, notifications):
    """A killed engine leaves no trace of its own and nobody reads a cron's
    exit status, so the timeout has to survive as a row."""
    _write_fake_script(cli_env["sync_dir"], exit_code=0, sleep=5)

    assert cli.main(["--timeout", "1"]) == 1

    runs = _rows(cli_env["db_path"], "SELECT * FROM sync_runs")
    assert len(runs) == 1
    assert runs[0]["exit_code"] == -1
    assert "timed out" in runs[0]["error"]


def test_default_timeout_leaves_headroom_under_the_cron_ceiling():
    """KiroCrew kills a command cron at 300s with nothing recorded. Timing
    out first is what converts that into a visible history row."""
    assert cli.DEFAULT_TIMEOUT_SECONDS < 300


# ---------------------------------------------------------------------------
# The invocation app.json actually uses
# ---------------------------------------------------------------------------

def test_app_json_cron_invokes_this_module_by_absolute_path():
    """A cron `command` runs via `sh -c` with the *gateway's* cwd and a
    scrubbed environment, so the invocation can lean on neither cwd nor
    PYTHONPATH -- it must name the interpreter and this file absolutely."""
    manifest = json.loads((cli._APP_DIR / "app.json").read_text(encoding="utf-8"))
    crons = manifest["crons"]
    assert len(crons) == 1
    command = crons[0]["command"]

    assert "backend/cli.py" in command
    # The per-app venv, not a bare `python3`: models.py needs pydantic, which
    # is only guaranteed to exist in the venv the gateway builds from
    # requirements.txt.
    assert ".venv/bin/python3" in command
    assert "$HOME/.kiro/crew/apps/kirocrew-sync/" in command
    # Command substitution is refused by the gateway's cron vetting
    # (mcp_cron._vet_shell_command); a plain $NAME reference is not.
    assert "$(" not in command and "`" not in command
    # And it must no longer be the old form, which bypassed this app entirely.
    assert "kirocrew-sync.sh" not in command


def _subprocess_env(cli_env, home):
    home.mkdir(exist_ok=True)
    return {
        **os.environ,
        # HOME too, not just the KIROCREW_* vars: notifications.py resolves the
        # app secret from Path.home() at import time, and this app really is
        # installed on the developing machine -- conftest's _fake_home patches
        # Path.home() in *this* process, which a child does not inherit.
        "HOME": str(home),
        "KIROCREW_SYNC_DIR": str(cli_env["sync_dir"]),
        "KIROCREW_DIR": str(cli_env["kirocrew_dir"]),
        "KIROCREW_SYNC_DB": str(cli_env["db_path"]),
    }


def test_running_the_file_directly_works_end_to_end(cli_env, tmp_path):
    """`python3 .../backend/cli.py` from an unrelated cwd -- the exact form
    app.json's cron uses. Run that way the module is __main__ with no parent
    package, so nothing but the sys.path bootstrap makes its `backend.*`
    imports resolve. No in-process test can cover this: importing cli.py as
    part of the `backend` package is precisely the case that already works.
    """
    _write_fake_script(cli_env["sync_dir"], stdout_text="Recorded local changes", exit_code=0)

    proc = subprocess.run(
        [sys.executable, str(cli._APP_DIR / "backend" / "cli.py"), "--strategy", "auto"],
        capture_output=True, text=True, timeout=60,
        env=_subprocess_env(cli_env, tmp_path / "script_home"),
        cwd=str(tmp_path),  # not the app dir -- a cron inherits the gateway's
    )

    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    runs = _rows(cli_env["db_path"], "SELECT * FROM sync_runs")
    assert len(runs) == 1
    assert runs[0]["exit_code"] == 0


def test_running_as_a_module_works_too(cli_env, tmp_path):
    """`python3 -m backend.cli` from the app root -- the form a developer
    reaches for, which must keep working alongside the bare-path one."""
    _write_fake_script(cli_env["sync_dir"], stdout_text="Sync complete", exit_code=0)

    proc = subprocess.run(
        [sys.executable, "-m", "backend.cli"],
        capture_output=True, text=True, timeout=60,
        env=_subprocess_env(cli_env, tmp_path / "module_home"),
        cwd=str(cli._APP_DIR),
    )

    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert len(_rows(cli_env["db_path"], "SELECT * FROM sync_runs")) == 1
