"""Row-level and structural three-way merge, exposed as git merge drivers.

Convergence requirement
-----------------------
Machine A merging B must produce byte-identical output to B merging A.
Otherwise the two machines rewrite each other's state forever and every sync
reports changes. Every automatic resolution below is therefore symmetric:
timestamps decide first, and ties fall back to comparing canonical JSON, which
does not depend on which side happens to be "ours".

The explicit --strategy local-wins / remote-wins options are asymmetric by
design; they are a user override, not the default path.
"""

import json
import os
from datetime import datetime, timezone

from . import policy as pol
from .canon import dumps, dumps_pretty, row_identity

AUTO = "auto"
LOCAL_WINS = "local-wins"
REMOTE_WINS = "remote-wins"
MANUAL = "manual"

STRATEGIES = (AUTO, LOCAL_WINS, REMOTE_WINS, MANUAL)


def parse_ts(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # The schemas mix naive and offset-aware timestamps; treat naive as UTC so
    # the two are comparable.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _tiebreak(ours, theirs):
    """Deterministic, side-independent winner."""
    return ours if dumps(ours) >= dumps(theirs) else theirs


def _newer(ours, theirs, ts_col):
    """LWW on ts_col, falling back to a deterministic tiebreak."""
    if ts_col:
        a, b = parse_ts(ours.get(ts_col)), parse_ts(theirs.get(ts_col))
        if a is not None and b is not None and a != b:
            return ours if a > b else theirs
        # Unparseable or equal: string order is still better than nothing.
        sa, sb = str(ours.get(ts_col) or ""), str(theirs.get(ts_col) or "")
        if sa != sb:
            return ours if sa > sb else theirs
    return _tiebreak(ours, theirs)


def index_rows(rows, identity):
    return {row_identity(r, identity): r for r in rows}


def merge_table(base, ours, theirs, table_policy, identity, strategy=AUTO,
                table="?"):
    """Three-way merge of three {identity_key: row} maps.

    Returns (merged_rows, conflicts). Conflicts are reported even when they
    were resolved automatically, so the user can see what happened.
    """
    conflicts = []
    merged = {}

    for key in set(base) | set(ours) | set(theirs):
        b, o, t = base.get(key), ours.get(key), theirs.get(key)
        bj = dumps(b) if b is not None else None
        oj = dumps(o) if o is not None else None
        tj = dumps(t) if t is not None else None

        if oj == tj:                      # both sides agree (incl. both deleted)
            winner = o
        elif bj == oj:                    # only remote touched it
            winner = t
        elif bj == tj:                    # only local touched it
            winner = o
        elif o is None or t is None:
            # One side deleted while the other edited. Keeping the edit is the
            # no-data-loss choice, and it converges: the deleting side adopts
            # the edit on its next merge.
            surviving = o if o is not None else t
            if strategy == LOCAL_WINS:
                winner = o
            elif strategy == REMOTE_WINS:
                winner = t
            elif strategy == MANUAL:
                conflicts.append(_conflict(table, key, "delete/modify", "unresolved"))
                return None, conflicts
            else:
                winner = surviving
            conflicts.append(_conflict(
                table, key, "delete/modify",
                "kept deletion" if winner is None else "kept edit"))
        else:
            # Genuine divergent edit to the same row.
            if strategy == LOCAL_WINS:
                winner = o
            elif strategy == REMOTE_WINS:
                winner = t
            elif strategy == MANUAL:
                conflicts.append(_conflict(table, key, "edit/edit", "unresolved"))
                return None, conflicts
            elif table_policy.mode == pol.UNION:
                winner = _tiebreak(o, t)
            else:
                winner = _newer(o, t, table_policy.ts_col)
            conflicts.append(_conflict(
                table, key, "edit/edit",
                "kept local" if winner is o else "kept remote"))

        if winner is not None:
            merged[key] = winner

    ordered = [merged[k] for k in sorted(merged)]
    return ordered, conflicts


def _conflict(table, key, kind, resolution):
    return {"table": table, "key": key, "kind": kind, "resolution": resolution}


# --------------------------------------------------------------------------
# Structural JSON merge (config.json, tags.json, ...)
# --------------------------------------------------------------------------

def merge_json(base, ours, theirs, strategy=AUTO, path="", conflicts=None):
    """Deep three-way merge. Lists are treated as atomic values."""
    if conflicts is None:
        conflicts = []

    if dumps(ours) == dumps(theirs):
        return ours, conflicts
    if dumps(base) == dumps(ours):
        return theirs, conflicts
    if dumps(base) == dumps(theirs):
        return ours, conflicts

    if isinstance(ours, dict) and isinstance(theirs, dict):
        base_d = base if isinstance(base, dict) else {}
        out = {}
        for key in set(ours) | set(theirs):
            here = path + "." + key if path else key
            if key not in ours:
                if key in base_d and dumps(base_d[key]) == dumps(theirs[key]):
                    continue          # local deleted it, remote left it alone
                out[key] = theirs[key]
            elif key not in theirs:
                if key in base_d and dumps(base_d[key]) == dumps(ours[key]):
                    continue          # remote deleted it, local left it alone
                out[key] = ours[key]
            else:
                out[key], _ = merge_json(base_d.get(key), ours[key],
                                         theirs[key], strategy, here, conflicts)
        return out, conflicts

    if strategy == LOCAL_WINS:
        winner = ours
    elif strategy == REMOTE_WINS:
        winner = theirs
    elif strategy == MANUAL:
        conflicts.append(_conflict(path or "<root>", path, "value", "unresolved"))
        return ours, conflicts
    else:
        winner = _tiebreak(ours, theirs)
    conflicts.append(_conflict(path or "<root>", path, "value",
                               "kept local" if winner is ours else "kept remote"))
    return winner, conflicts


# --------------------------------------------------------------------------
# git merge driver entry points
# --------------------------------------------------------------------------

def _read_jsonl_or_die(path, label):
    from .dbio import read_jsonl
    try:
        return read_jsonl(path)
    except ValueError as exc:
        raise SystemExit("kcsync: %s: %s" % (label, exc))


def _load_policy(repo_root, rel_path):
    """Resolve the policy for db/<name>/<table>.jsonl."""
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) < 3 or parts[0] != "db":
        return None, None, None
    db_name, table = parts[1], parts[-1][:-len(".jsonl")]

    policy_file = os.path.join(repo_root, "db", db_name, "_policy.json")
    identity = None
    table_policy = pol.OVERRIDES.get(db_name, {}).get(table)
    try:
        with open(policy_file, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        entry = stored.get(table)
        if entry:
            identity = entry.get("identity")
            if table_policy is None:
                table_policy = pol.TablePolicy.from_json(
                    {k: v for k, v in entry.items() if k != "identity"})
    except (OSError, ValueError):
        pass

    if table_policy is None:
        table_policy = pol.TablePolicy(pol.UNION, note="unknown table")
    return table_policy, identity, table


def _infer_identity(rows):
    """Last-resort identity when _policy.json is unavailable."""
    for candidate in (["id"], ["key"], ["uri"]):
        if rows and all(c in rows[0] for c in candidate):
            return candidate
    return sorted(rows[0].keys()) if rows else []


def driver_rows(argv):
    """git merge driver for db/**/*.jsonl. argv: <ancestor> <ours> <theirs> <path>"""
    ancestor, ours_file, theirs_file, rel_path = argv[:4]
    strategy = os.environ.get("KCSYNC_STRATEGY", AUTO)
    repo_root = os.environ.get("KCSYNC_REPO", os.getcwd())

    table_policy, identity, table = _load_policy(repo_root, rel_path)
    base_rows = _read_jsonl_or_die(ancestor, "ancestor " + rel_path)
    our_rows = _read_jsonl_or_die(ours_file, "ours " + rel_path)
    their_rows = _read_jsonl_or_die(theirs_file, "theirs " + rel_path)

    if not identity:
        identity = _infer_identity(our_rows or their_rows or base_rows)

    merged, conflicts = merge_table(
        index_rows(base_rows, identity),
        index_rows(our_rows, identity),
        index_rows(their_rows, identity),
        table_policy, identity, strategy, table or rel_path)

    _record_conflicts(rel_path, conflicts)

    if merged is None:
        return 1                       # manual strategy: leave it to the user

    with open(ours_file, "w", encoding="utf-8") as fh:
        for row in merged:
            fh.write(dumps(row) + "\n")
    return 0


def driver_json(argv):
    """git merge driver for files/**/*.json."""
    ancestor, ours_file, theirs_file, rel_path = argv[:4]
    strategy = os.environ.get("KCSYNC_STRATEGY", AUTO)

    def load(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
            return json.loads(text) if text.strip() else {}
        except ValueError as exc:
            raise SystemExit("kcsync: %s is not valid JSON: %s" % (path, exc))

    merged, conflicts = merge_json(load(ancestor), load(ours_file),
                                   load(theirs_file), strategy)
    _record_conflicts(rel_path, conflicts)
    if strategy == MANUAL and conflicts:
        return 1
    with open(ours_file, "w", encoding="utf-8") as fh:
        fh.write(dumps_pretty(merged))
    return 0


def _record_conflicts(rel_path, conflicts):
    if not conflicts:
        return
    log_path = os.environ.get("KCSYNC_CONFLICT_LOG")
    if not log_path:
        return
    with open(log_path, "a", encoding="utf-8") as fh:
        for c in conflicts:
            entry = dict(c)
            entry["path"] = rel_path
            fh.write(dumps(entry) + "\n")
