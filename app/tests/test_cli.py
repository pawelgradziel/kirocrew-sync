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
