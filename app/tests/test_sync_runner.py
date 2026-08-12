"""
Tests for backend/sync_runner.py's `run_sync_and_record` -- specifically the
change-derivation wiring added on top of the pre-existing sync_runs/
conflicts/quarantine recording (see test_cli.py and test_server.py for the
exit-code/outcome contract, which this file does not repeat).

These exercise the real SyncManager/HistoryManager/ConflictManager/
QuarantineManager stack against a fake kirocrew-sync.sh (a small bash
script standing in for the real engine, same technique as
test_sync_manager.py's `_write_fake_script`) so the derived sync_changes
rows can be verified end-to-end: real subprocess, real sqlite, real
conflicts.jsonl parsing.

SAFETY: every path below is tmp_path-derived; conftest.py's autouse
fixtures additionally guard against ever touching ~/.kiro/crew.
"""

import json
from pathlib import Path

from backend.conflicts import ConflictManager
from backend.history import HistoryManager
from backend.quarantine import QuarantineManager
from backend.sync_manager import SyncManager
from backend.sync_runner import run_sync_and_record


def _write_fake_script(sync_dir: Path, stdout_lines, exit_code: int = 0) -> None:
    sync_dir.mkdir(parents=True, exist_ok=True)
    script = sync_dir / "kirocrew-sync.sh"
    lines = ["#!/usr/bin/env bash"]
    for line in stdout_lines:
        lines.append(f'echo "{line}"')
    lines.append(f"exit {exit_code}")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)


def _make_managers(tmp_path, sync_dir, kirocrew_dir):
    db_path = tmp_path / "data" / "history.db"
    sync_mgr = SyncManager(sync_dir=sync_dir, kirocrew_dir=kirocrew_dir, db_path=db_path)
    # SyncManager.is_running() shells out to a system-wide `pgrep -f
    # "kirocrew-sync.sh sync"` -- not scoped to this test's own subprocess in
    # any way. On a real dev machine that can and does match an unrelated
    # kirocrew-sync.sh process (a live daemon, another test run, another
    # engineer's shell), which would make run_sync_and_record() skip the run
    # entirely (already_running=True) for reasons that have nothing to do
    # with what this file tests. Forced deterministic here so these tests
    # exercise change derivation, not this machine's process table.
    sync_mgr.is_running = lambda: False
    history_mgr = HistoryManager(db_path=db_path)
    conflict_mgr = ConflictManager(db_path=db_path)
    quarantine_mgr = QuarantineManager(db_path=db_path)
    return sync_mgr, history_mgr, conflict_mgr, quarantine_mgr


def _write_conflicts_jsonl(kirocrew_dir: Path, records) -> None:
    sync_root = kirocrew_dir / ".sync"
    sync_root.mkdir(parents=True, exist_ok=True)
    path = sync_root / "conflicts.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def test_run_sync_and_record_derives_and_persists_changes(tmp_path):
    sync_dir = tmp_path / "sync"
    kirocrew_dir = tmp_path / "kirocrew"
    _write_fake_script(
        sync_dir,
        [
            "ℹ Unpacking local state...",
            "✓ Recorded local changes",
            "ℹ Applying merged state to KiroCrew...",
            "  packed memory: 9 rows written, 0 deleted",
            "  packed knowledge: 4 rows written, 1 deleted",
            "  packed 2 files",
            "✓ Sync complete",
        ],
        exit_code=0,
    )
    _write_conflicts_jsonl(kirocrew_dir, [
        {"table": "items", "key": "item-7", "kind": "edit/edit", "resolution": "kept local"},
        # memory.db bookkeeping table -- must not be recorded (see artifacts.py).
        {"table": "memory_events", "key": "e1", "kind": "edit/edit", "resolution": "kept local"},
    ])

    sync_mgr, history_mgr, conflict_mgr, quarantine_mgr = _make_managers(
        tmp_path, sync_dir, kirocrew_dir
    )

    outcome = run_sync_and_record(
        sync_mgr, history_mgr, conflict_mgr, quarantine_mgr,
        strategy="auto", team=False, dry_run=False, timeout=30,
    )

    assert outcome.started is True
    assert outcome.result.exit_code == 0
    assert outcome.run_id is not None

    details = history_mgr.get_run_details(outcome.run_id)
    assert details is not None

    # knowledge.db pack: written + deleted rows, and the one classifiable
    # conflict. memory.db's aggregate and the memory_events conflict are
    # both deliberately absent -- see artifacts.py's module comment.
    by_action = {(c.change_type, c.action): c for c in details.changes}
    assert set(by_action) == {
        ("knowledge", "updated"),
        ("knowledge", "deleted"),
        ("knowledge", "conflict"),
    }
    assert "4 row(s)" in by_action[("knowledge", "updated")].details
    assert "1 row(s)" in by_action[("knowledge", "deleted")].details
    conflict_change = by_action[("knowledge", "conflict")]
    assert conflict_change.item_id == "item-7"
    assert "kept local" in conflict_change.details

    # And this is exactly what GET /api/history/{id} would now hand back
    # instead of an empty list.
    assert len(details.changes) == 3


def test_run_sync_and_record_no_derivable_detail_records_zero_rows(tmp_path):
    """A sync that changed nothing derivable (no pack lines, no conflicts)
    must record zero sync_changes rows -- not a placeholder row claiming
    something happened."""
    sync_dir = tmp_path / "sync"
    kirocrew_dir = tmp_path / "kirocrew"
    _write_fake_script(
        sync_dir,
        [
            "ℹ Unpacking local state...",
            "ℹ No local changes since last sync",
            "ℹ No remote machines found",
            "✓ Sync complete",
        ],
        exit_code=0,
    )
    # No conflicts.jsonl written at all -- mirrors a machine that has never
    # hit a conflict.

    sync_mgr, history_mgr, conflict_mgr, quarantine_mgr = _make_managers(
        tmp_path, sync_dir, kirocrew_dir
    )

    outcome = run_sync_and_record(
        sync_mgr, history_mgr, conflict_mgr, quarantine_mgr,
        strategy="auto", team=False, dry_run=False, timeout=30,
    )

    assert outcome.started is True
    details = history_mgr.get_run_details(outcome.run_id)
    assert details.changes == []


def test_run_sync_and_record_failed_run_before_truncation_skips_derivation(tmp_path):
    """An engine failure that never reached cmd_sync()'s truncation (e.g. a
    bad --strategy value, exit 1 before "Unpacking local state...") must not
    read -- and re-record as this run's own -- a previous run's leftover
    conflicts.jsonl."""
    sync_dir = tmp_path / "sync"
    kirocrew_dir = tmp_path / "kirocrew"
    _write_fake_script(sync_dir, ["✗ invalid --strategy value"], exit_code=1)
    # Leftover from a hypothetical previous run.
    _write_conflicts_jsonl(kirocrew_dir, [
        {"table": "items", "key": "stale-1", "kind": "edit/edit", "resolution": "kept local"},
    ])

    sync_mgr, history_mgr, conflict_mgr, quarantine_mgr = _make_managers(
        tmp_path, sync_dir, kirocrew_dir
    )

    outcome = run_sync_and_record(
        sync_mgr, history_mgr, conflict_mgr, quarantine_mgr,
        strategy="auto", team=False, dry_run=False, timeout=30,
    )

    assert outcome.result.exit_code == 1
    details = history_mgr.get_run_details(outcome.run_id)
    assert details.changes == []
