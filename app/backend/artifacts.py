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
