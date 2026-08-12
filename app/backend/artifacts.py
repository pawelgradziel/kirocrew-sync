"""
Artifact ingestion - reads the on-disk logs written by the bash sync engine
(kirocrew-sync.sh / lib/kcsync/merge.py) and turns them into structured data
the rest of the app can consume.

Two logs matter here, both scope-dependent (see scope_paths() in
kirocrew-sync.sh) and both truncated at the start of every `cmd_sync()` run,
so they only ever describe the most recently completed run:

- conflicts.jsonl / conflicts-team.jsonl: one JSON object per line, written
  by _record_conflicts() in lib/kcsync/merge.py. Shape:
      {"table": ..., "key": ..., "kind": "edit/edit" | "delete/modify" | "value",
       "resolution": "unresolved" | "kept local" | "kept remote" |
                     "kept deletion" | "kept edit",
       "path": <relative file path>}
- quarantine.txt / quarantine-team.txt: one machine id per line.

All parsing here is best-effort: a missing file, an empty file, or a
malformed/partial line is tolerated (skipped) rather than raised, because
these files are written by a separate process (the bash engine) that this
app does not control the timing or integrity of.
"""

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

from .models import SyncChange

logger = logging.getLogger(__name__)


def _truncate(text: str, limit: int = 200) -> str:
    """Cap a raw log line before it goes into a log message -- a
    pathological or truncated-mid-write conflicts.jsonl file could
    otherwise dump an unbounded amount of text into the log."""
    return text if len(text) <= limit else text[:limit] + "...(truncated)"

# Maps a conflicts.jsonl "resolution" string (see _conflict() in
# lib/kcsync/merge.py) to the (resolved, resolution) pair the app's
# `conflicts` table CHECK constraint accepts:
#   resolution IN ('local-wins', 'remote-wins', 'manual')
RESOLUTION_MAP: Dict[str, tuple] = {
    "unresolved": (False, None),
    "kept local": (True, "local-wins"),
    "kept remote": (True, "remote-wins"),
    "kept deletion": (True, "manual"),
    "kept edit": (True, "manual"),
}

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")
_SQUEEZE_RE = re.compile(r"-{2,}")


@dataclass
class ConflictRecord:
    """One parsed line from conflicts.jsonl."""
    table: str
    key: str
    kind: str
    resolution: str
    path: Optional[str] = None

    def resolved_pair(self) -> tuple:
        """(resolved: bool, resolution: Optional[str]) for the conflicts table."""
        return RESOLUTION_MAP.get(self.resolution, (False, None))


def scope_paths(sync_root: Path, team: bool) -> Dict[str, Path]:
    """
    Mirror scope_paths() in kirocrew-sync.sh: each scope gets its own repo,
    conflict log and quarantine log under the same SYNC_ROOT.
    """
    if team:
        return {
            "repo": sync_root / "repo-team",
            "conflicts": sync_root / "conflicts-team.jsonl",
            "quarantine": sync_root / "quarantine-team.txt",
        }
    return {
        "repo": sync_root / "repo",
        "conflicts": sync_root / "conflicts.jsonl",
        "quarantine": sync_root / "quarantine.txt",
    }


def read_conflicts(path: Path) -> List[ConflictRecord]:
    """
    Parse conflicts.jsonl. Tolerates a missing file, an empty file, and
    malformed/partial lines -- bad lines are skipped, never raised, but
    every skip is logged at warning level with enough context (file, line
    number, and a truncated copy of the line) to diagnose it. Silently
    dropping a line here previously meant a sync that crashed mid-write
    (leaving a truncated trailing JSON line, for instance) lost that
    conflict with no signal anywhere -- not in this app, not in a log, not
    anywhere a user or developer would ever see it.
    """
    records: List[ConflictRecord] = []
    if not path.exists():
        return records
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not read conflicts file %s: %s", path, exc)
        return records

    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "Skipping malformed line %d in %s (invalid JSON: %s): %s",
                line_no, path, exc, _truncate(line),
            )
            continue
        if not isinstance(data, dict):
            logger.warning(
                "Skipping line %d in %s: JSON value is not an object: %s",
                line_no, path, _truncate(line),
            )
            continue

        table = data.get("table")
        key = data.get("key")
        resolution = data.get("resolution")
        if table is None or key is None or resolution is None:
            missing = [
                name for name, value in
                (("table", table), ("key", key), ("resolution", resolution))
                if value is None
            ]
            logger.warning(
                "Skipping line %d in %s: missing required field(s) %s: %s",
                line_no, path, ", ".join(missing), _truncate(line),
            )
            continue

        kind = data.get("kind")
        path_value = data.get("path")
        records.append(ConflictRecord(
            table=str(table),
            key=str(key),
            kind=str(kind) if kind is not None else "",
            resolution=str(resolution),
            path=path_value if isinstance(path_value, str) else None,
        ))
    return records


def read_quarantine(path: Path) -> List[str]:
    """
    Parse quarantine.txt: one machine id per line. Tolerates a missing file
    and an empty file.
    """
    machines: List[str] = []
    if not path.exists():
        return machines
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return machines

    for line in text.splitlines():
        line = line.strip()
        if line:
            machines.append(line)
    return machines


def sanitize_machine_id(raw: str) -> str:
    """
    Reproduce get_machine_id()'s sanitization in kirocrew-sync.sh:
        tr -c 'A-Za-z0-9._-' '-' | sed 's/-\\{2,\\}/-/g; s/^-//; s/-$//'
    """
    sanitized = _SANITIZE_RE.sub("-", raw)
    sanitized = _SQUEEZE_RE.sub("-", sanitized)
    return sanitized.strip("-")


def get_local_machine_id(kirocrew_dir: Path) -> str:
    """
    Best-effort local machine id, read from $KIROCREW_DIR/.machine_id.

    conflicts.jsonl records do not name a machine at all (see _conflict() in
    lib/kcsync/merge.py -- only table/key/kind/resolution are recorded), so
    this is the closest available stand-in for "whose conflict is this": the
    machine running this app is also the machine that ran the merge that
    produced the log. Returns "unknown" if the file is absent, empty, or
    unreadable, rather than raising or inventing an id.
    """
    machine_file = kirocrew_dir / ".machine_id"
    try:
        raw = machine_file.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    if not raw:
        return "unknown"
    sanitized = sanitize_machine_id(raw)
    return sanitized or "unknown"


def count_active_machines(repo: Path, quarantined: Set[str], timeout: float = 10.0) -> int:
    """
    Count distinct remote machine refs (refs/remotes/<machine>/<branch>) in
    the scope's sync repo, excluding quarantined ones. Mirrors how
    merge_remote_refs() in kirocrew-sync.sh enumerates remotes. Returns 0,
    never raises, when the repo does not exist yet or git is unavailable.
    """
    if not (repo / ".git").exists():
        return 0
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "for-each-ref", "--format=%(refname)", "refs/remotes"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return 0
    if result.returncode != 0:
        return 0

    machines: Set[str] = set()
    for line in result.stdout.splitlines():
        # refs/remotes/<machine>/<branch>
        parts = line.strip().split("/")
        if len(parts) >= 3 and parts[0] == "refs" and parts[1] == "remotes":
            machines.add(parts[2])
    return len(machines - quarantined)


# ---------------------------------------------------------------------------
# Deriving app/backend/sync_changes rows.
#
# The engine reports change detail at exactly two granularities and no finer
# than that -- verified against lib/kcsync/cli.py, lib/kcsync/dbio.py,
# lib/kcsync/merge.py and lib/kcsync/policy.py:
#
#   1. Per-database row counts, from cmd_pack()'s
#      "packed <db>: N rows written, M deleted" line (cli.py:129-130).
#      dbio.pack_db() sums "applied"/"deleted" across every table it packs
#      in that one database (dbio.py:255-339) and returns/prints no
#      per-table breakdown. "N rows written" is also an unconditional
#      INSERT OR REPLACE count for every row present in the database's
#      canonical export (dbio.py:303-316) -- it is not a count of rows that
#      actually differ from what was already on disk, so it cannot honestly
#      be reported as "N items added".
#   2. Per-conflict records (ConflictRecord, from conflicts.jsonl / merge.py
#      _conflict()), each naming one real table (or, for a JSON file
#      conflict, one real relative path -- see driver_json()/_conflict()
#      with kind="value") and how it was resolved.
#
# Neither ever names an individual knowledge item, lesson, or transcript, so
# nothing here invents one either. Everything below maps to the *type*
# database.py's sync_changes.change_type CHECK constraint already knows
# ('knowledge', 'artifact', 'lesson', 'transcript', 'config'), never finer,
# and anything that cannot be honestly attributed to exactly one of those
# five is left unclassified (dropped) rather than guessed.
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# lib/kcsync/cli.py cmd_pack()'s log line, one per database in
# lib/kcsync/policy.py DATABASES ("memory" -> memory.db, "knowledge" ->
# workspace/knowledge/knowledge.db).
_PACK_LINE_RE = re.compile(
    r"packed\s+(\S+):\s*(\d+)\s+rows written,\s*(\d+)\s+deleted"
)

# Only "knowledge" maps to a single change_type. "memory" is deliberately
# absent: policy.py's OVERRIDES["memory"] packs semantic_memory ("Learned
# lessons", policy.py:70-72 -- this app's "lesson"), episodic_memories (raw
# conversation content, policy.py:73-76 -- closest to "transcript"), and
# machine bookkeeping (memory_events/memory_meta/schema_version) into ONE
# aggregate "packed memory: N rows written" line with no way to split the
# count back apart by table. Recording that aggregate under "lesson" or
# "transcript" would misattribute rows from the other tables riding along
# in the same pack -- so it is not recorded as a per-database change at all;
# any memory.db conflict still gets classified per-table below, where the
# table name IS known.
DB_TO_CHANGE_TYPE: Dict[str, str] = {
    "knowledge": "knowledge",
}

# Real DB table -> change_type, for conflict records where `kind` names an
# actual table (i.e. not a JSON-file conflict; see _classify_conflict()).
# Table names and their meaning are exactly policy.py's OVERRIDES.
TABLE_TO_CHANGE_TYPE: Dict[str, str] = {
    # knowledge.db (policy.py OVERRIDES["knowledge"])
    "sources": "knowledge",
    "items": "knowledge",
    "entities": "knowledge",
    "entity_relations": "knowledge",
    "mentions": "knowledge",
    "source_locations": "knowledge",
    "artifact_item_state": "knowledge",
    "dismissed_auto_sources": "knowledge",
    "folder_file_state": "knowledge",
    "ingestion_jobs": "knowledge",
    # memory.db (policy.py OVERRIDES["memory"])
    "semantic_memory": "lesson",        # "Learned lessons" -- policy.py:70-72
    "episodic_memories": "transcript",  # raw conversation content -- policy.py:73-76
    # memory_events / memory_meta / schema_version / sqlite_sequence:
    # deliberately absent. Event log / metadata / SQLite bookkeeping, not
    # "lesson" or "transcript" content -- classifying them as either would
    # be a guess, not a mapping.
}

# JSON-file conflicts (merge.py driver_json(), kind="value") record the
# relative path as `table` -- see files.py's ALLOW list for what each of
# these actually is.
_CONFIG_FILE_NAMES = frozenset({
    "config.json", "tags.json", "tag_boards.json", "session_map.json",
    "admission_policy.json", "model_windows.json", "autonudge.json",
    "hooks.json",
})


def _classify_path(path: str) -> Optional[str]:
    """change_type for a JSON-file conflict's path, or None if it does not
    map cleanly to exactly one of the five change_type values. files.py's
    ALLOW list also covers "workspace/*.md" and "workspace/memory/**" --
    both left unclassified here, since a bare relative path under
    workspace/ does not, by itself, say whether that note is a "lesson",
    project documentation, or something else entirely."""
    if path in _CONFIG_FILE_NAMES:
        return "config"
    if path.startswith("artifacts/"):
        return "artifact"
    if path.startswith("sessions/"):
        return "transcript"
    return None


def _classify_conflict(record: ConflictRecord) -> Optional[str]:
    """change_type for one parsed conflicts.jsonl record, or None if it
    cannot be honestly classified (see TABLE_TO_CHANGE_TYPE / _classify_path
    docstrings for what is deliberately left out)."""
    if record.kind == "value":
        return _classify_path(record.table)
    return TABLE_TO_CHANGE_TYPE.get(record.table)


def parse_pack_stats(output: str) -> Dict[str, Dict[str, int]]:
    """
    Parse every "packed <db>: N rows written, M deleted" line out of a
    sync run's combined stdout+stderr (SyncResult.output). Returns
    {db_name: {"applied": N, "deleted": M}} for whichever databases the
    line actually shows -- a database that was never packed (e.g. the run
    failed before apply_to_kirocrew()) has no entry at all, never a
    fabricated {"applied": 0, "deleted": 0}.
    """
    clean = _strip_ansi(output or "")
    stats: Dict[str, Dict[str, int]] = {}
    for db_name, applied, deleted in _PACK_LINE_RE.findall(clean):
        entry = stats.setdefault(db_name, {"applied": 0, "deleted": 0})
        entry["applied"] += int(applied)
        entry["deleted"] += int(deleted)
    return stats


def derive_sync_changes(
    output: str, conflict_records: List[ConflictRecord]
) -> List[SyncChange]:
    """
    Build the SyncChange rows one sync run's output and parsed conflicts
    honestly support -- see the module-level comment above for exactly
    what is and is not derivable. Returns [] when neither source yields
    anything classifiable, which is itself an honest answer (a dry run, a
    run that changed nothing, or a run whose only pack was memory.db).
    """
    changes: List[SyncChange] = []

    stats = parse_pack_stats(output)
    for db_name, change_type in DB_TO_CHANGE_TYPE.items():
        entry = stats.get(db_name)
        if entry is None:
            continue
        if entry["applied"] > 0:
            changes.append(SyncChange(
                change_type=change_type,
                action="updated",
                item_id=None,
                details=(
                    f"{entry['applied']} row(s) written to {db_name}.db "
                    "during pack (an upsert count: includes rows "
                    "re-written unchanged, not only rows that actually "
                    "differ from before)"
                ),
            ))
        if entry["deleted"] > 0:
            changes.append(SyncChange(
                change_type=change_type,
                action="deleted",
                item_id=None,
                details=f"{entry['deleted']} row(s) deleted from {db_name}.db during pack",
            ))

    for record in conflict_records:
        change_type = _classify_conflict(record)
        if change_type is None:
            continue
        changes.append(SyncChange(
            change_type=change_type,
            action="conflict",
            item_id=record.key,
            details=f"{record.kind} conflict on {record.table}, resolution: {record.resolution}",
        ))

    return changes
