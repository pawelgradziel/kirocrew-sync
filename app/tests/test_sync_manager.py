"""
Tests for backend/sync_manager.py:
- _parse_output() / _extract_error() / _artifact_counts() against realistic
  kirocrew-sync.sh output (see kirocrew-sync.sh's cmd_sync(),
  merge_remote_refs(), report_conflicts() and lib/kcsync/cli.py's
  cmd_pack() for the ground-truth strings these are derived from)
- the exit-code contract (0 ok, 3 quarantined-but-completed, 1 failed)
- daemon config read/write round trip through daemon_state, incl. interval
  bounds
- next-sync is always None (the daemon does not follow this app's schedule
  at all -- see get_next_sync()'s docstring)
- daemon PID safety: PID<=0 refused, identity confirmed before signalling,
  stale lock cleared after a forced kill, waitpid() scoped to our own
  children
- lazy path resolution / degraded state when the engine is absent
"""

import os
import subprocess
import time
from datetime import datetime

import pytest

from backend import artifacts
from backend.sync_manager import SyncEngineUnavailable, SyncManager, _resolve_kirocrew_dir


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_fake_script(sync_dir, stdout_text, exit_code, stderr_text=""):
    sync_dir.mkdir(parents=True, exist_ok=True)
    script = sync_dir / "kirocrew-sync.sh"
    lines = ["#!/usr/bin/env bash", f'echo "{stdout_text}"']
    if stderr_text:
        lines.append(f'echo "{stderr_text}" >&2')
    lines.append(f"exit {exit_code}")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)
    return script


def make_manager(tmp_path, sync_dir=None, kirocrew_dir=None):
    return SyncManager(
        sync_dir=sync_dir if sync_dir is not None else tmp_path / "sync",
        kirocrew_dir=kirocrew_dir if kirocrew_dir is not None else tmp_path / "kirocrew",
        db_path=tmp_path / "data" / "history.db",
    )


# ---------------------------------------------------------------------------
# _parse_output()
#
# NOTE: these replace an earlier version of this test file that fed
# _parse_output() invented strings like "✓ Synced 3 tables, 42 rows" and
# "No changes detected. Already up to date." -- patterns that appear
# nowhere in kirocrew-sync.sh. That encoded the exact bug being fixed here:
# _parse_output()'s old `(\d+)\s+rows?` regex happened to match the digit
# in report_conflicts()'s real "N row conflict(s) resolved automatically"
# line (kirocrew-sync.sh:364), reporting the *conflict* count as rows
# merged, and its "No changes"/"up to date" short-circuit matched strings
# the engine never emits at all. The tests below use the engine's actual
# output strings instead (see kirocrew-sync.sh's cmd_sync()/
# merge_remote_refs()/report_conflicts() and lib/kcsync/cli.py's
# cmd_pack()).
# ---------------------------------------------------------------------------

def test_parse_output_full_success_sums_rows_from_pack_lines(tmp_path):
    mgr = make_manager(tmp_path)
    output = (
        "ℹ Unpacking local state...\n"
        "✓ Recorded local changes\n"
        "ℹ Fetching from gdrive...\n"
        "✓ Fetched 2 remote machine(s)\n"
        "✓ Merged 2 remote machine(s)\n"
        "ℹ Applying merged state to KiroCrew...\n"
        "  packed memory: 30 rows written, 2 deleted\n"
        "  packed knowledge: 12 rows written, 0 deleted\n"
        "ℹ Publishing to gdrive...\n"
        "✓ Sync complete\n"
    )
    parsed = mgr._parse_output(output)
    assert parsed["changes_detected"] is True
    assert parsed["rows_merged"] == 42  # 30 + 12, summed across both databases


def test_parse_output_genuinely_idle_run_reports_no_changes(tmp_path):
    """The real 'nothing happened' output: no local diff, no remote
    machines. Must not be confused with a successful sync just because
    exit_code is 0 -- exit 0 covers both cases identically, so the fixed
    implementation never looks at exit_code at all."""
    output = (
        "ℹ Unpacking local state...\n"
        "ℹ No local changes since last sync\n"
        "ℹ Fetching from gdrive...\n"
        "ℹ No remote machines found\n"
        "ℹ Applying merged state to KiroCrew...\n"
        "  packed memory: 0 rows written, 0 deleted\n"
        "  packed knowledge: 0 rows written, 0 deleted\n"
        "✓ Sync complete\n"
    )
    mgr = make_manager(tmp_path)
    parsed = mgr._parse_output(output)
    assert parsed["changes_detected"] is False
    assert parsed["rows_merged"] == 0


def test_parse_output_quarantine_with_merge_still_detects_changes(tmp_path):
    output = (
        "✓ Recorded local changes\n"
        "✓ Fetched 3 remote machine(s)\n"
        "✓ Merged 2 remote machine(s)\n"
        "⚠ 1 machine(s) quarantined; their changes were not merged:\n"
        "    old-laptop\n"
        "ℹ Applying merged state to KiroCrew...\n"
        "  packed memory: 10 rows written, 0 deleted\n"
        "⚠ Sync complete, with 1 machine(s) still quarantined\n"
    )
    mgr = make_manager(tmp_path)
    parsed = mgr._parse_output(output)
    assert parsed["changes_detected"] is True
    assert parsed["rows_merged"] == 10


def test_parse_output_strips_ansi_before_matching(tmp_path):
    """log_success()/log_info() etc. (kirocrew-sync.sh:32-35) always wrap
    their glyph in ANSI colour codes -- there is no TTY check -- so real
    captured output looks like this, not like plain text."""
    output = (
        "\x1b[0;34mℹ\x1b[0m Unpacking local state...\n"
        "\x1b[0;32m✓\x1b[0m Recorded local changes\n"
        "\x1b[0;34mℹ\x1b[0m Applying merged state to KiroCrew...\n"
        "  packed memory: 5 rows written, 0 deleted\n"
    )
    mgr = make_manager(tmp_path)
    parsed = mgr._parse_output(output)
    assert parsed["changes_detected"] is True
    assert parsed["rows_merged"] == 5


def test_parse_output_pack_never_ran_defaults_rows_to_zero(tmp_path):
    """A run that fails before apply_to_kirocrew() (e.g. unresolved git
    conflicts) never prints a 'packed ...' line at all. rows_merged=0 here
    is the type system's floor (SyncResult.rows_merged is a non-Optional
    int), not a claim that zero rows were verified written -- see
    run_sync()'s error handling for how failures are actually surfaced."""
    output = (
        "✗ Unresolved conflicts. Sync stopped before touching your data.\n"
        "    db/knowledge/notes.jsonl\n"
    )
    mgr = make_manager(tmp_path)
    parsed = mgr._parse_output(output)
    assert parsed["changes_detected"] is False
    assert parsed["rows_merged"] == 0


# ---------------------------------------------------------------------------
# _artifact_counts() -- conflicts_count/quarantine_count sourced from
# conflicts.jsonl/quarantine.txt directly, not regexed out of prose.
# ---------------------------------------------------------------------------

def test_artifact_counts_reads_scope_specific_files(tmp_path):
    mgr = make_manager(tmp_path)
    mgr.sync_root.mkdir(parents=True, exist_ok=True)
    paths = artifacts.scope_paths(mgr.sync_root, team=False)
    paths["conflicts"].write_text(
        '{"table": "knowledge", "key": "k1", "kind": "value", "resolution": "unresolved"}\n'
        '{"table": "knowledge", "key": "k2", "kind": "value", "resolution": "kept local"}\n'
    )
    paths["quarantine"].write_text("machine-a\nmachine-b\n")

    team_paths = artifacts.scope_paths(mgr.sync_root, team=True)
    team_paths["conflicts"].write_text(
        '{"table": "knowledge", "key": "k9", "kind": "value", "resolution": "unresolved"}\n'
    )

    conflicts_count, quarantine_count = mgr._artifact_counts(team=False)
    assert conflicts_count == 2
    assert quarantine_count == 2

    # Team scope reads its own files, not personal's.
    team_conflicts, team_quarantine = mgr._artifact_counts(team=True)
    assert team_conflicts == 1
    assert team_quarantine == 0


def test_artifact_counts_missing_files_are_zero_not_an_error(tmp_path):
    mgr = make_manager(tmp_path)
    assert mgr._artifact_counts(team=False) == (0, 0)


# ---------------------------------------------------------------------------
# _extract_error()
# ---------------------------------------------------------------------------

def test_extract_error_none_for_success(tmp_path):
    mgr = make_manager(tmp_path)
    assert mgr._extract_error("✓ Sync complete\n", exit_code=0) is None


def test_extract_error_none_for_exit_3_even_though_nonzero(tmp_path):
    """Exit code 3 is success-with-quarantine, not a failure -- it must
    never populate error, even when the output contains a ⚠ warning."""
    mgr = make_manager(tmp_path)
    output = "⚠ Sync complete, with 1 machine(s) still quarantined\n"
    assert mgr._extract_error(output, exit_code=3) is None


def test_extract_error_extracts_log_error_lines_stripping_ansi(tmp_path):
    mgr = make_manager(tmp_path)
    output = (
        "\x1b[0;34mℹ\x1b[0m Unpacking local state...\n"
        "\x1b[0;31m✗\x1b[0m A previous sync left unresolved conflicts.\n"
        "\x1b[0;34mℹ\x1b[0m Resolve them and run './kirocrew-sync.sh resume'.\n"
    )
    error = mgr._extract_error(output, exit_code=1)
    assert error == "✗ A previous sync left unresolved conflicts."


def test_extract_error_falls_back_to_raw_output_without_log_error_lines(tmp_path):
    """A genuine crash (Python traceback, `set -e` abort) can land outside
    any log_error() call entirely."""
    mgr = make_manager(tmp_path)
    output = "Traceback (most recent call last):\nboom: disk full\n"
    error = mgr._extract_error(output, exit_code=1)
    assert "boom: disk full" in error


def test_extract_error_none_when_output_is_empty(tmp_path):
    mgr = make_manager(tmp_path)
    assert mgr._extract_error("", exit_code=1) is None


# ---------------------------------------------------------------------------
# run_sync() exit-code contract, exercised end to end against a fake script
# ---------------------------------------------------------------------------

def test_run_sync_missing_script_raises_sync_engine_unavailable(tmp_path):
    mgr = make_manager(tmp_path)  # sync_dir has no kirocrew-sync.sh
    assert mgr.available is False
    with pytest.raises(SyncEngineUnavailable):
        mgr.run_sync()


def test_run_sync_exit_0_is_ok(tmp_path):
    sync_dir = tmp_path / "sync"
    output = (
        "✓ Recorded local changes\n"
        "ℹ Applying merged state to KiroCrew...\n"
        "  packed memory: 5 rows written, 0 deleted\n"
        "✓ Sync complete\n"
    )
    _write_fake_script(sync_dir, output, 0)
    mgr = make_manager(tmp_path, sync_dir=sync_dir)

    result = mgr.run_sync(strategy="auto", team=False, dry_run=False, timeout=10)

    assert result.exit_code == 0
    assert result.scope == "personal"
    assert result.strategy == "auto"
    assert result.rows_merged == 5
    assert result.changes_detected is True
    assert result.conflicts_count == 0
    assert result.quarantine_count == 0
    assert result.error is None
    assert result.duration_ms >= 0


def test_run_sync_exit_3_is_quarantined_but_completed(tmp_path):
    sync_dir = tmp_path / "sync"
    output = (
        "✓ Recorded local changes\n"
        "✓ Fetched 2 remote machine(s)\n"
        "✓ Merged 1 remote machine(s)\n"
        "⚠ 1 machine(s) quarantined; their changes were not merged:\n"
        "    old-laptop\n"
        "ℹ Applying merged state to KiroCrew...\n"
        "  packed memory: 2 rows written, 0 deleted\n"
        "⚠ Sync complete, with 1 machine(s) still quarantined\n"
    )
    _write_fake_script(sync_dir, output, 3)
    mgr = make_manager(tmp_path, sync_dir=sync_dir)

    # conflicts_count/quarantine_count are read straight out of
    # conflicts.jsonl/quarantine.txt (what the real engine would have left
    # behind by the time the process exits), not regexed from the prose
    # above -- see _artifact_counts().
    mgr.sync_root.mkdir(parents=True, exist_ok=True)
    artifacts.scope_paths(mgr.sync_root, team=True)["quarantine"].write_text("old-laptop\n")

    result = mgr.run_sync(team=True)

    assert result.exit_code == 3
    assert result.scope == "team"
    assert result.changes_detected is True
    assert result.rows_merged == 2
    assert result.quarantine_count == 1
    assert result.conflicts_count == 0
    assert result.error is None  # exit 3 is success-with-quarantine, not a failure


def test_run_sync_exit_1_surfaces_log_error_text_from_stdout(tmp_path):
    """The engine's log_error() writes to stdout, not stderr (see
    kirocrew-sync.sh:32-35), so the real diagnostic text lives in
    result.stdout, not result.stderr -- this is what the original
    `error = result.stderr if returncode != 0 else None` got wrong."""
    sync_dir = tmp_path / "sync"
    output = (
        "ℹ Unpacking local state...\n"
        "✗ A previous sync left unresolved conflicts.\n"
        "ℹ Resolve them and run './kirocrew-sync.sh resume'.\n"
    )
    _write_fake_script(sync_dir, output, 1)
    mgr = make_manager(tmp_path, sync_dir=sync_dir)

    result = mgr.run_sync()

    assert result.exit_code == 1
    assert result.changes_detected is False
    assert result.rows_merged == 0
    assert result.error is not None
    assert "A previous sync left unresolved conflicts" in result.error


def test_run_sync_exit_1_without_log_error_line_falls_back_to_raw_output(tmp_path):
    """A genuine crash outside any log_error() call (e.g. a `set -e`
    abort) still needs to surface *some* diagnostic text; this is the
    scenario the original stderr-based implementation was aimed at, just
    via the wrong stream."""
    sync_dir = tmp_path / "sync"
    _write_fake_script(sync_dir, "", 1, stderr_text="boom: disk full")
    mgr = make_manager(tmp_path, sync_dir=sync_dir)

    result = mgr.run_sync()

    assert result.exit_code == 1
    assert result.changes_detected is False
    assert result.error is not None
    assert "boom: disk full" in result.error


# ---------------------------------------------------------------------------
# Daemon config read/write round trip
# ---------------------------------------------------------------------------

def test_get_daemon_config_defaults(tmp_path):
    mgr = make_manager(tmp_path)
    config = mgr.get_daemon_config()
    assert config == {"enabled": True, "scope": "personal", "interval": 300}


def test_update_daemon_config_round_trips(tmp_path):
    mgr = make_manager(tmp_path)
    updated = mgr.update_daemon_config(enabled=False, scope="team", interval=120)
    assert updated == {"enabled": False, "scope": "team", "interval": 120}

    # A fresh read (new call, re-querying daemon_state) must agree.
    assert mgr.get_daemon_config() == {"enabled": False, "scope": "team", "interval": 120}


def test_update_daemon_config_partial_update_preserves_other_fields(tmp_path):
    mgr = make_manager(tmp_path)
    mgr.update_daemon_config(enabled=False, scope="team", interval=200)
    mgr.update_daemon_config(interval=250)  # only interval changes
    config = mgr.get_daemon_config()
    assert config == {"enabled": False, "scope": "team", "interval": 250}


@pytest.mark.parametrize("interval", [59, 901, 0, -10])
def test_update_daemon_config_rejects_out_of_range_interval(tmp_path, interval):
    mgr = make_manager(tmp_path)
    with pytest.raises(ValueError):
        mgr.update_daemon_config(interval=interval)


@pytest.mark.parametrize("interval", [60, 900])
def test_update_daemon_config_accepts_boundary_interval(tmp_path, interval):
    mgr = make_manager(tmp_path)
    updated = mgr.update_daemon_config(interval=interval)
    assert updated["interval"] == interval


def test_update_daemon_config_rejects_invalid_scope(tmp_path):
    mgr = make_manager(tmp_path)
    with pytest.raises(ValueError):
        mgr.update_daemon_config(scope="bogus")


# ---------------------------------------------------------------------------
# next-sync computation
#
# get_next_sync() always returns None now: lib/daemon.sh drives its own
# adaptive polling loop (INTERVAL_IDLE/ACTIVE/BACKOFF, chosen per-cycle) and
# never reads this app's daemon_state table at all, so `last_run + interval`
# was a plausible-looking number with no causal connection to when the
# daemon will actually run -- a guess presented as fact. The
# `test_get_next_sync_is_last_run_plus_interval` case below replaces an
# earlier version of this test that asserted exactly that fabricated value;
# it's kept (renamed) specifically to prove the fix, not just deleted.
# ---------------------------------------------------------------------------

def test_get_next_sync_none_when_disabled(tmp_path):
    mgr = make_manager(tmp_path)
    mgr.update_daemon_config(enabled=False)
    mgr.record_daemon_run(timestamp=datetime(2026, 1, 1, 12, 0, 0))
    assert mgr.get_next_sync() is None


def test_get_next_sync_none_when_never_run(tmp_path):
    mgr = make_manager(tmp_path)
    mgr.update_daemon_config(enabled=True)
    assert mgr.get_next_sync() is None


def test_get_next_sync_none_even_with_enabled_daemon_and_recorded_last_run(tmp_path):
    """Previously this asserted next_sync == last_run + interval. That was
    the bug: lib/daemon.sh does not follow this app's configured interval
    at all (grep-verified -- it never reads daemon_state), so no matter how
    complete the inputs look (enabled, a real last_run, a real interval),
    the daemon's actual next run time is genuinely unknown to this app."""
    mgr = make_manager(tmp_path)
    mgr.update_daemon_config(enabled=True, interval=120)
    last_run = datetime(2026, 3, 1, 9, 0, 0)
    mgr.record_daemon_run(timestamp=last_run)

    assert mgr.get_next_sync() is None


# ---------------------------------------------------------------------------
# Lazy path resolution / degraded state when the engine is absent
# ---------------------------------------------------------------------------

def test_manager_construction_never_raises_without_engine_installed(tmp_path):
    """A machine without the bash sync engine installed must still get a
    working (degraded) app -- construction itself must never raise."""
    mgr = make_manager(tmp_path)
    assert mgr.available is False
    # Non-sync operations keep working in the degraded state.
    assert mgr.get_daemon_config() == {"enabled": True, "scope": "personal", "interval": 300}
    status = mgr.get_status()
    assert status.machines_active == 0
    assert status.machines_quarantined == 0
    assert status.conflicts_pending == 0


def test_start_daemon_requires_script(tmp_path):
    mgr = make_manager(tmp_path)
    with pytest.raises(SyncEngineUnavailable):
        mgr.start_daemon()


def test_available_true_once_script_exists(tmp_path):
    sync_dir = tmp_path / "sync"
    _write_fake_script(sync_dir, "ok", 0)
    mgr = make_manager(tmp_path, sync_dir=sync_dir)
    assert mgr.available is True


def test_resolve_kirocrew_dir_env_var_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_DIR", str(tmp_path / "env-dir"))
    result = _resolve_kirocrew_dir(tmp_path / "unused-sync-dir")
    assert result == tmp_path / "env-dir"


def test_resolve_kirocrew_dir_reads_config_sh_with_home_expansion(tmp_path, monkeypatch):
    monkeypatch.delenv("KIROCREW_DIR", raising=False)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "config.sh").write_text('export KIROCREW_DIR="$HOME/.kiro/crew"\n')

    result = _resolve_kirocrew_dir(sync_dir)

    # Path.home() is patched by the autouse _fake_home fixture, so this
    # proves real $HOME expansion happened rather than a literal string.
    from pathlib import Path
    assert result == Path.home() / ".kiro" / "crew"


def test_resolve_kirocrew_dir_defaults_when_nothing_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("KIROCREW_DIR", raising=False)
    sync_dir = tmp_path / "sync-with-no-config"
    sync_dir.mkdir()

    result = _resolve_kirocrew_dir(sync_dir)

    from pathlib import Path
    assert result == Path.home() / ".kiro" / "crew"


def test_sync_manager_default_paths_never_touch_real_home(tmp_path, monkeypatch):
    """Constructing SyncManager() with no args at all must still resolve
    everything under the (fixture-patched) fake home, never the real one."""
    monkeypatch.delenv("KIROCREW_SYNC_DIR", raising=False)
    monkeypatch.delenv("KIROCREW_DIR", raising=False)

    from pathlib import Path
    fake_home = Path.home()  # already patched by the autouse fixture
    mgr = SyncManager(db_path=tmp_path / "data" / "history.db")

    assert str(mgr.sync_dir).startswith(str(fake_home))
    assert str(mgr.kirocrew_dir).startswith(str(fake_home))
    assert mgr.available is False


# ---------------------------------------------------------------------------
# Daemon PID safety -- _read_daemon_pid()/_is_kirocrew_daemon_pid()/
# _pid_alive()/stop_daemon(). Exercised against real processes (not mocked
# os.kill), per the verification approach used to find these bugs: writing
# real PIDs into a real daemon.lock file and observing real behaviour.
# ---------------------------------------------------------------------------

def _write_fake_daemon_script(sync_dir, lock_path, ignore_term=False):
    """A stand-in for kirocrew-sync.sh that, when run as `<script> daemon`,
    writes its own PID to `lock_path` (mirroring lib/daemon.sh's
    acquire_daemon_lock(): `echo $$ > "$DAEMON_LOCK"`) and then sleeps.
    `ignore_term=True` makes it immune to SIGTERM, forcing stop_daemon()
    down its SIGKILL escalation path."""
    sync_dir.mkdir(parents=True, exist_ok=True)
    script = sync_dir / "kirocrew-sync.sh"
    trap_line = "trap '' TERM" if ignore_term else ""
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'echo $$ > "{lock_path}"\n'
        f"{trap_line}\n"
        "sleep 300\n"
    )
    script.chmod(0o755)
    return script


def _wait_until_gone(pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("bad_content", ["0", "-1", "not-a-number", "", "   "])
def test_read_daemon_pid_rejects_invalid_lock_content(tmp_path, bad_content):
    """A corrupt lock file containing 0, a negative number, or garbage must
    never reach os.kill(): kill(0, sig) targets this whole process's
    process group (i.e. the app server itself) and kill(-1, sig) targets
    every process the caller can signal. Verified against the pre-fix
    implementation: it returned 0/-1 straight through, because its only
    check was `kill(pid, 0)`, which "succeeds" for both without raising
    (sig=0 is a permission probe, not an identity check)."""
    mgr = make_manager(tmp_path)
    mgr.sync_root.mkdir(parents=True, exist_ok=True)
    mgr._daemon_lock_path().write_text(bad_content)

    assert mgr._read_daemon_pid() is None
    assert mgr.daemon_status() == {"running": False, "pid": None}


def test_daemon_status_and_stop_daemon_refuse_unrelated_live_process(tmp_path):
    """The core PID-reuse scenario: daemon.lock names a real, live PID that
    is not a kirocrew-sync.sh daemon at all. Verified against the pre-fix
    implementation: daemon_status() reported it running, and stop_daemon()
    SIGTERM'd/SIGKILL'd it while returning success=True -- an innocent
    process, terminated, with the app reporting a clean stop."""
    mgr = make_manager(tmp_path)
    mgr.sync_root.mkdir(parents=True, exist_ok=True)

    innocent = subprocess.Popen(["sleep", "300"])
    try:
        mgr._daemon_lock_path().write_text(str(innocent.pid))

        # Identity can't be confirmed -> treated as "no daemon running",
        # not as "some process is running that we'll assume is ours".
        assert mgr.daemon_status() == {"running": False, "pid": None}

        stopped, message = mgr.stop_daemon(timeout=0.3)
        assert stopped is True
        assert "not running" in message.lower()

        # The innocent process must be completely untouched.
        assert innocent.poll() is None
    finally:
        innocent.terminate()
        innocent.wait(timeout=5)


def test_start_daemon_then_stop_daemon_terminates_real_daemon_process(tmp_path):
    """The happy path, end to end: a real (fake) kirocrew-sync.sh daemon
    process, correctly identified and cleanly stopped."""
    sync_dir = tmp_path / "sync"
    mgr = make_manager(tmp_path, sync_dir=sync_dir)
    lock_path = mgr._daemon_lock_path()
    _write_fake_daemon_script(sync_dir, lock_path)

    started, start_message = mgr.start_daemon(wait=2.0)
    assert started, start_message

    status = mgr.daemon_status()
    assert status["running"] is True
    pid = status["pid"]
    assert pid is not None and pid > 0

    stopped, stop_message = mgr.stop_daemon(timeout=2.0)
    assert stopped, stop_message
    assert _wait_until_gone(pid), f"PID {pid} should have been terminated"


def test_stop_daemon_escalates_to_sigkill_and_clears_stale_lock(tmp_path):
    """A daemon whose TERM trap can't run in time (modelled here by simply
    ignoring TERM, standing in for lib/daemon.sh's real cause: the trap is
    deferred behind a foreground `sleep` that can run up to
    INTERVAL_BACKOFF=600s -- see stop_daemon()'s docstring) must be
    force-killed rather than reported as stopped when it demonstrably is
    not. After SIGKILL, no trap runs at all (SIGKILL is never caught), so
    the app must clean up the now-stale lock file itself."""
    sync_dir = tmp_path / "sync"
    mgr = make_manager(tmp_path, sync_dir=sync_dir)
    lock_path = mgr._daemon_lock_path()
    _write_fake_daemon_script(sync_dir, lock_path, ignore_term=True)

    started, start_message = mgr.start_daemon(wait=2.0)
    assert started, start_message
    pid = mgr.daemon_status()["pid"]
    assert pid is not None

    stopped, stop_message = mgr.stop_daemon(timeout=1.0)
    assert stopped, stop_message
    assert "force-killed" in stop_message
    assert _wait_until_gone(pid), f"PID {pid} should have been SIGKILLed"
    assert not lock_path.exists(), "stale lock must be removed after a forced kill"


def test_pid_alive_does_not_reap_processes_it_did_not_launch(tmp_path):
    """_pid_alive() must only call os.waitpid() on PIDs this instance
    itself launched via start_daemon() (tracked in
    self._daemon_child_pids). Verified against the pre-fix implementation:
    it called os.waitpid(pid, WNOHANG) on *any* PID read from the lock
    file. Here, a process this test (not the manager) is directly
    responsible for reaping is checked via _pid_alive() first; if that
    call reaped it, this test's own wait() below would get ECHILD instead
    of the real exit status."""
    mgr = make_manager(tmp_path)
    proc = subprocess.Popen(["sleep", "300"])
    try:
        assert proc.pid not in mgr._daemon_child_pids
        assert mgr._pid_alive(proc.pid) is True  # alive, and not reaped
    finally:
        proc.terminate()
        # If _pid_alive() had wrongly reaped this child, this would raise
        # ChildProcessError instead of returning the real exit status.
        exit_code = proc.wait(timeout=5)
        assert exit_code is not None
