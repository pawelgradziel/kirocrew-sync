"""
Tests for backend/artifacts.py -- parsing conflicts.jsonl and quarantine.txt
written by the bash sync engine. This is the highest-value target in this
suite: these files are the sole interface between an external, untrusted-
format process (bash/kcsync/merge.py) and this app's database, so parsing
must be exactly as forgiving as documented (missing file, empty file,
malformed/partial lines all tolerated) while still extracting every
documented resolution mapping correctly.
"""

import json
import subprocess

import pytest

from backend import artifacts
from backend.artifacts import ConflictRecord


# ---------------------------------------------------------------------------
# resolved_pair() / RESOLUTION_MAP
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "resolution,expected",
    [
        ("unresolved", (False, None)),
        ("kept local", (True, "local-wins")),
        ("kept remote", (True, "remote-wins")),
        ("kept deletion", (True, "manual")),
        ("kept edit", (True, "manual")),
    ],
)
def test_resolved_pair_documented_mapping(resolution, expected):
    record = ConflictRecord(table="knowledge", key="k1", kind="edit/edit", resolution=resolution)
    assert record.resolved_pair() == expected


def test_resolved_pair_unknown_resolution_defaults_to_unresolved():
    """A resolution string the app doesn't recognize must not corrupt the
    conflicts table's CHECK constraint by falling through with a garbage
    resolution value -- it must default to (False, None), same as
    'unresolved'."""
    record = ConflictRecord(table="knowledge", key="k1", kind="value", resolution="something new")
    assert record.resolved_pair() == (False, None)


# ---------------------------------------------------------------------------
# read_conflicts()
# ---------------------------------------------------------------------------

def test_read_conflicts_missing_file_returns_empty(tmp_path):
    assert artifacts.read_conflicts(tmp_path / "does-not-exist.jsonl") == []


def test_read_conflicts_empty_file_returns_empty(tmp_path):
    path = tmp_path / "conflicts.jsonl"
    path.write_text("")
    assert artifacts.read_conflicts(path) == []


def test_read_conflicts_parses_every_documented_resolution(tmp_path):
    lines = [
        {"table": "knowledge", "key": "k1", "kind": "edit/edit", "resolution": "unresolved", "path": "a.md"},
        {"table": "knowledge", "key": "k2", "kind": "edit/edit", "resolution": "kept local", "path": "b.md"},
        {"table": "lessons", "key": "l1", "kind": "delete/modify", "resolution": "kept remote"},
        {"table": "lessons", "key": "l2", "kind": "delete/modify", "resolution": "kept deletion"},
        {"table": "artifacts", "key": "a1", "kind": "value", "resolution": "kept edit", "path": "c.md"},
    ]
    path = tmp_path / "conflicts.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

    records = artifacts.read_conflicts(path)
    assert len(records) == 5
    assert [r.resolution for r in records] == [
        "unresolved", "kept local", "kept remote", "kept deletion", "kept edit",
    ]
    # Non-vacuous field checks, not just counts/resolutions.
    assert records[0].table == "knowledge"
    assert records[0].key == "k1"
    assert records[0].kind == "edit/edit"
    assert records[0].path == "a.md"
    # path omitted -> None, not "None" or missing attribute.
    assert records[2].path is None
    assert records[2].table == "lessons"
    assert records[2].key == "l1"


def test_read_conflicts_skips_malformed_lines_without_raising(tmp_path):
    """Malformed/partial lines must be skipped, and -- critically -- must
    not corrupt parsing of the well-formed lines around them. The
    'non-vacuous' guard: we assert the good lines survive with correct
    content, not merely that the bad ones are absent (which would also be
    true of a parser that silently returned [] for everything)."""
    good_1 = json.dumps({"table": "knowledge", "key": "good-1", "kind": "value", "resolution": "kept local"})
    good_2 = json.dumps({"table": "knowledge", "key": "good-2", "kind": "value", "resolution": "kept remote"})
    lines = [
        good_1,
        "not json at all {{{",
        "",
        "   ",
        json.dumps([1, 2, 3]),                                  # valid JSON, not an object
        json.dumps({"key": "missing-table", "resolution": "unresolved"}),   # missing table
        json.dumps({"table": "t", "resolution": "unresolved"}),             # missing key
        json.dumps({"table": "t", "key": "missing-resolution"}),            # missing resolution
        json.dumps({"table": "t", "key": "null-resolution", "resolution": None}),  # explicit null
        '{"table": "t", "key": "truncated"',                     # truncated JSON
        good_2,
    ]
    path = tmp_path / "conflicts.jsonl"
    path.write_text("\n".join(lines) + "\n")

    records = artifacts.read_conflicts(path)

    assert len(records) == 2
    assert records[0].key == "good-1"
    assert records[0].resolution == "kept local"
    assert records[1].key == "good-2"
    assert records[1].resolution == "kept remote"


def test_read_conflicts_logs_skipped_malformed_lines(tmp_path, caplog):
    """Skipped lines must not be silent: a sync that crashed mid-write
    (leaving a truncated trailing JSON line, for instance) previously lost
    that conflict with zero signal anywhere. Each skip is now logged at
    warning level with the file, line number, and a copy of the offending
    line, so it is at least diagnosable after the fact."""
    good = json.dumps({"table": "knowledge", "key": "good", "kind": "value", "resolution": "kept local"})
    lines = [
        good,
        "not json at all {{{",
        json.dumps({"key": "missing-table", "resolution": "unresolved"}),
        '{"table": "t", "key": "truncated"',
    ]
    path = tmp_path / "conflicts.jsonl"
    path.write_text("\n".join(lines) + "\n")

    with caplog.at_level("WARNING", logger="backend.artifacts"):
        records = artifacts.read_conflicts(path)

    assert len(records) == 1
    assert records[0].key == "good"

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 3  # one per skipped line, not per file
    joined = "\n".join(r.getMessage() for r in warnings)
    assert str(path) in joined
    # Line numbers (1-indexed) let a developer find the exact bad line.
    assert "line 2" in joined  # "not json at all {{{"
    assert "line 3" in joined  # missing table
    assert "line 4" in joined  # truncated JSON
    assert "table" in joined  # names the missing field for the line-3 case


def test_read_conflicts_kind_defaults_to_empty_string_when_absent(tmp_path):
    path = tmp_path / "conflicts.jsonl"
    path.write_text(json.dumps({"table": "t", "key": "k", "resolution": "unresolved"}) + "\n")
    records = artifacts.read_conflicts(path)
    assert len(records) == 1
    assert records[0].kind == ""


def test_read_conflicts_non_string_path_becomes_none(tmp_path):
    path = tmp_path / "conflicts.jsonl"
    path.write_text(json.dumps({"table": "t", "key": "k", "resolution": "unresolved", "path": 123}) + "\n")
    records = artifacts.read_conflicts(path)
    assert records[0].path is None


# ---------------------------------------------------------------------------
# read_quarantine()
# ---------------------------------------------------------------------------

def test_read_quarantine_missing_file_returns_empty(tmp_path):
    assert artifacts.read_quarantine(tmp_path / "does-not-exist.txt") == []


def test_read_quarantine_empty_file_returns_empty(tmp_path):
    path = tmp_path / "quarantine.txt"
    path.write_text("")
    assert artifacts.read_quarantine(path) == []


def test_read_quarantine_parses_machine_ids_skipping_blank_lines(tmp_path):
    path = tmp_path / "quarantine.txt"
    path.write_text("machine-a\n\n   \nmachine-b\nmachine-c\n")
    machines = artifacts.read_quarantine(path)
    assert machines == ["machine-a", "machine-b", "machine-c"]


# ---------------------------------------------------------------------------
# scope_paths() -- scope-dependent path selection
# ---------------------------------------------------------------------------

def test_scope_paths_personal(tmp_path):
    paths = artifacts.scope_paths(tmp_path, team=False)
    assert paths["repo"] == tmp_path / "repo"
    assert paths["conflicts"] == tmp_path / "conflicts.jsonl"
    assert paths["quarantine"] == tmp_path / "quarantine.txt"


def test_scope_paths_team(tmp_path):
    paths = artifacts.scope_paths(tmp_path, team=True)
    assert paths["repo"] == tmp_path / "repo-team"
    assert paths["conflicts"] == tmp_path / "conflicts-team.jsonl"
    assert paths["quarantine"] == tmp_path / "quarantine-team.txt"


def test_scope_paths_personal_and_team_never_collide(tmp_path):
    personal = artifacts.scope_paths(tmp_path, team=False)
    team = artifacts.scope_paths(tmp_path, team=True)
    assert set(personal.values()).isdisjoint(set(team.values()))


# ---------------------------------------------------------------------------
# sanitize_machine_id()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("plain-machine.name_1", "plain-machine.name_1"),
        ("My Machine!!123", "My-Machine-123"),
        ("  weird@@name  ", "weird-name"),
        ("---leading-and-trailing---", "leading-and-trailing"),
        ("a///b", "a-b"),
    ],
)
def test_sanitize_machine_id(raw, expected):
    assert artifacts.sanitize_machine_id(raw) == expected


def test_sanitize_machine_id_all_disallowed_chars_yields_empty_string():
    assert artifacts.sanitize_machine_id("@@@") == ""


# ---------------------------------------------------------------------------
# get_local_machine_id()
# ---------------------------------------------------------------------------

def test_get_local_machine_id_missing_file_returns_unknown(tmp_path):
    kirocrew_dir = tmp_path / "kirocrew"
    kirocrew_dir.mkdir()
    assert artifacts.get_local_machine_id(kirocrew_dir) == "unknown"


def test_get_local_machine_id_empty_file_returns_unknown(tmp_path):
    kirocrew_dir = tmp_path / "kirocrew"
    kirocrew_dir.mkdir()
    (kirocrew_dir / ".machine_id").write_text("")
    assert artifacts.get_local_machine_id(kirocrew_dir) == "unknown"


def test_get_local_machine_id_sanitizes_content(tmp_path):
    kirocrew_dir = tmp_path / "kirocrew"
    kirocrew_dir.mkdir()
    (kirocrew_dir / ".machine_id").write_text("My Laptop!!\n")
    assert artifacts.get_local_machine_id(kirocrew_dir) == "My-Laptop"


def test_get_local_machine_id_only_disallowed_chars_returns_unknown(tmp_path):
    kirocrew_dir = tmp_path / "kirocrew"
    kirocrew_dir.mkdir()
    (kirocrew_dir / ".machine_id").write_text("@@@\n")
    assert artifacts.get_local_machine_id(kirocrew_dir) == "unknown"


# ---------------------------------------------------------------------------
# count_active_machines() -- exercised against a real git repo, not mocked
# ---------------------------------------------------------------------------

def _run_git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_repo_with_remote_refs(path, machine_branches):
    """machine_branches: {machine_id: [branch, ...]}"""
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q"], cwd=path)
    _run_git(["config", "user.email", "test@example.com"], cwd=path)
    _run_git(["config", "user.name", "Test"], cwd=path)
    _run_git(["commit", "-q", "--allow-empty", "-m", "init"], cwd=path)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(path), check=True, capture_output=True, text=True
    ).stdout.strip()
    for machine, branches in machine_branches.items():
        for branch in branches:
            _run_git(["update-ref", f"refs/remotes/{machine}/{branch}", sha], cwd=path)
    return path


def test_count_active_machines_no_git_repo_returns_zero(tmp_path):
    repo = tmp_path / "no-repo-here"
    assert artifacts.count_active_machines(repo, quarantined=set()) == 0


def test_count_active_machines_repo_with_no_remotes_returns_zero(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q"], cwd=repo)
    assert artifacts.count_active_machines(repo, quarantined=set()) == 0


def test_count_active_machines_counts_distinct_machines_across_branches(tmp_path):
    repo = _make_repo_with_remote_refs(
        tmp_path / "repo",
        {"machine-a": ["main", "feature"], "machine-b": ["main"], "machine-c": ["main"]},
    )
    # machine-a has two branches but must count once.
    assert artifacts.count_active_machines(repo, quarantined=set()) == 3


def test_count_active_machines_excludes_quarantined(tmp_path):
    repo = _make_repo_with_remote_refs(
        tmp_path / "repo",
        {"machine-a": ["main"], "machine-b": ["main"], "machine-c": ["main"]},
    )
    assert artifacts.count_active_machines(repo, quarantined={"machine-b"}) == 2


# ---------------------------------------------------------------------------
# parse_pack_stats() -- cli.py cmd_pack()'s
# "packed <db>: N rows written, M deleted" lines
# ---------------------------------------------------------------------------

def test_parse_pack_stats_realistic_two_database_output():
    output = (
        "\x1b[0;32mℹ Applying merged state to KiroCrew...\x1b[0m\n"
        "  packed memory: 30 rows written, 2 deleted\n"
        "  packed knowledge: 12 rows written, 0 deleted\n"
        "  packed 4 files\n"
    )
    stats = artifacts.parse_pack_stats(output)
    assert stats == {
        "memory": {"applied": 30, "deleted": 2},
        "knowledge": {"applied": 12, "deleted": 0},
    }


def test_parse_pack_stats_no_pack_lines_returns_empty():
    # A run that failed before apply_to_kirocrew() (unresolved conflicts,
    # blocked gate, KiroCrew still running) never reaches cmd_pack() at all.
    output = "✗ A previous sync left unresolved conflicts.\n"
    assert artifacts.parse_pack_stats(output) == {}


def test_parse_pack_stats_empty_output_returns_empty():
    assert artifacts.parse_pack_stats("") == {}
    assert artifacts.parse_pack_stats(None) == {}


# ---------------------------------------------------------------------------
# derive_sync_changes() -- the sync_changes rows a run's output and parsed
# conflicts honestly support.
# ---------------------------------------------------------------------------

def test_derive_sync_changes_knowledge_pack_written_and_deleted():
    output = "  packed knowledge: 5 rows written, 2 deleted\n"
    changes = artifacts.derive_sync_changes(output, [])
    assert len(changes) == 2
    written, deleted = changes
    assert written.change_type == "knowledge"
    assert written.action == "updated"
    assert written.item_id is None
    assert "5 row(s)" in written.details
    assert deleted.change_type == "knowledge"
    assert deleted.action == "deleted"
    assert "2 row(s)" in deleted.details


def test_derive_sync_changes_memory_pack_is_not_recorded():
    """memory.db's aggregate count mixes lesson/transcript/bookkeeping
    tables with no way to split it back apart -- see the module-level
    comment in artifacts.py. It must not be guessed into either category,
    or dropped as though nothing happened silently-wrongly attributed."""
    output = "  packed memory: 30 rows written, 2 deleted\n"
    assert artifacts.derive_sync_changes(output, []) == []


def test_derive_sync_changes_zero_counts_produce_no_rows():
    output = "  packed knowledge: 0 rows written, 0 deleted\n"
    assert artifacts.derive_sync_changes(output, []) == []


def test_derive_sync_changes_no_pack_and_no_conflicts_is_empty():
    """The honest answer for a dry run, or a run where nothing changed: zero
    rows, never a placeholder."""
    assert artifacts.derive_sync_changes("", []) == []


def test_derive_sync_changes_classifies_knowledge_table_conflict():
    record = ConflictRecord(table="items", key="item-42", kind="edit/edit", resolution="kept local")
    changes = artifacts.derive_sync_changes("", [record])
    assert len(changes) == 1
    change = changes[0]
    assert change.change_type == "knowledge"
    assert change.action == "conflict"
    assert change.item_id == "item-42"
    assert "edit/edit" in change.details
    assert "kept local" in change.details


def test_derive_sync_changes_classifies_lesson_and_transcript_tables():
    lesson = ConflictRecord(table="semantic_memory", key="lesson-1", kind="edit/edit", resolution="kept remote")
    transcript = ConflictRecord(table="episodic_memories", key="ep-1", kind="delete/modify", resolution="kept deletion")
    changes = artifacts.derive_sync_changes("", [lesson, transcript])
    assert [c.change_type for c in changes] == ["lesson", "transcript"]
    assert [c.action for c in changes] == ["conflict", "conflict"]


def test_derive_sync_changes_classifies_file_conflicts_by_path():
    config = ConflictRecord(table="config.json", key="config.json", kind="value", resolution="kept local")
    artifact = ConflictRecord(table="artifacts/foo.json", key="artifacts/foo.json", kind="value", resolution="unresolved")
    transcript = ConflictRecord(table="sessions/2024-01-01.jsonl", key="sessions/2024-01-01.jsonl", kind="value", resolution="kept edit")
    changes = artifacts.derive_sync_changes("", [config, artifact, transcript])
    assert [c.change_type for c in changes] == ["config", "artifact", "transcript"]


def test_derive_sync_changes_drops_unclassifiable_conflicts():
    """memory.db bookkeeping tables and bare workspace/*.md paths cannot be
    honestly attributed to any single change_type -- they are dropped, not
    guessed."""
    bookkeeping = ConflictRecord(table="memory_events", key="e1", kind="edit/edit", resolution="kept local")
    workspace_note = ConflictRecord(table="workspace/notes.md", key="workspace/notes.md", kind="value", resolution="kept remote")
    assert artifacts.derive_sync_changes("", [bookkeeping, workspace_note]) == []


def test_derive_sync_changes_combines_pack_and_conflicts():
    output = (
        "  packed knowledge: 3 rows written, 0 deleted\n"
        "  packed memory: 10 rows written, 0 deleted\n"
    )
    conflict = ConflictRecord(table="sources", key="src-1", kind="edit/edit", resolution="kept local")
    changes = artifacts.derive_sync_changes(output, [conflict])
    assert len(changes) == 2
    assert changes[0].change_type == "knowledge" and changes[0].action == "updated"
    assert changes[1].change_type == "knowledge" and changes[1].action == "conflict"
