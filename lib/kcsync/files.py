"""Plain-file sync: allowlist, hard denylist, and secret redaction.

The KiroCrew directory mixes portable state with credentials, PID files, lock
files, 30 MB local audit logs and a checked-out git repo. Syncing it by
exclusion is a losing game, so this uses an explicit allowlist. The denylist
exists on top of it as a backstop: it always wins, so a future allowlist entry
cannot accidentally widen to cover a key file.
"""

import fnmatch
import json
import re
import shutil
from pathlib import Path

from . import policy as pol
from . import stores
from .canon import dumps_pretty

# Only these ever leave the machine.
# Glob rules (path-aware, not raw fnmatch): * and ? never cross '/', ** spans
# directories. So "sessions/*.jsonl" is top-level only; use ** for recursion.
ALLOW = [
    "config.json",
    "tags.json",
    "tag_boards.json",
    "admission_policy.json",
    "model_windows.json",
    "autonudge.json",
    "hooks.json",
    "sessions/*.jsonl",
    # KiroCrew rolls the older turns of a long conversation out of the live
    # transcript into `sessions/archive/<key>__<stamp>.jsonl` (history.py).
    # Without this line the live half of a long session crosses machines and
    # its older half does not, so the same conversation reads as truncated on
    # the second machine. Listed explicitly because the glob above is
    # deliberately non-recursive.
    "sessions/archive/*.jsonl",
    "workspace/*.md",
    "workspace/memory/**",
    "artifacts/**",
    # The plain-file half of each memory store under memory_stores/<name>/
    # (see stores.py for the layout). The store's memory.db is synced row by
    # row as its own logical database, never as a file. Store names are also
    # validated (stores.file_store_ok), so `*` can never stand for "..", a
    # dot-directory or the unreachable "default".
    "memory_stores/*/memory/*.md",           # preferences.md, projects.md
    "memory_stores/*/memory/history/*.md",   # named V1 store's daily history
    "memory_stores/*/lessons.jsonl",         # named V1 store's lesson file
    "memory_stores/*/member-memory.json",    # legacy ownership manifest
]

# Deliberately absent, though upstream KiroCrew's own backup component set
# includes both: crons.json and notifications.jsonl. Not an oversight --
# a synced crons.json would make every machine fire the same scheduled jobs,
# so two machines would each run (and duplicate-notify for) work meant to
# happen once. notifications.jsonl is that same delivery history, which is
# per-machine noise for the same reason: a notification fired on machine A
# is not a fact about machine B. See README's "Deliberately not synced" table.

# The subset of ALLOW that travels in team scope. Same opt-in rule as tables
# (see policy.py): anything not named here stays on the machine, so a file
# added to ALLOW later never reaches colleagues until someone decides it should.
# Chat transcripts and every per-person config file are deliberately absent.
TEAM_ALLOW = [
    "tags.json",
    "tag_boards.json",
    "artifacts/**",
]

# Always wins over ALLOW.
#
# session_map.json is machine-local, like upstream's own portable export treats
# it (portability.EXPORT_EXCLUDE). Each entry points at a kiro-cli context in
# ~/.kiro/sessions/cli, which is outside the sync root, and KiroCrew's startup
# SessionMap.prune() deletes every entry whose <sid>.json it cannot find. So a
# synced copy was pruned on every other machine, and the three-way merge then
# carried that deletion back and cost the originating machine its own resume
# mappings. Listed here rather than just dropped from ALLOW so a later ALLOW
# glob cannot reintroduce it; pack never deletes local files, so every machine
# keeps its own copy. See docs/upstream-sync-review-2026-09-22.md.
DENY = [
    "session_map.json",
    "**/.git/**", "**/.git",
    "**/*.lock", "**/*.tmp", "**/*.bak", "**/*~", "**/*.sig",
    "**/*.key", "**/*.pem", "**/*.credentials", "**/*.token",
    "**/*secret*", "**/*_secret", "**/.local_secret",
    "**/*.db", "**/*.db-wal", "**/*.db-shm",
    ".machine_id", "beacon_install_id", "beacon_last_sent", "telemetry_salt",
    "kiro_pids.txt", "kiro_session_pids.txt", "session_pid_*",
    "security_events.jsonl", "audit.log", "gateway.log", "gateway.log.prev",
    # Holds live MCP server credentials.
    "mcp.json",
    "run/**", "cache/**", "logs/**", "imports/**", "uploads/**",
    "cron-history/**", "usage/**", "models/**", "apps/**", "skills/**",
    ".migrations/**",
    # Host-local state inside memory_stores/, exactly upstream's
    # memory_stores.is_host_local_store_state: the historical member API key,
    # the member backup/restore directory (which also holds the namespace and
    # store-use lock files), execution logs, and a named store's own rolling
    # backups. None of it is memory; a backup restored on another machine
    # would roll that machine's store back to this one's past. A store's
    # memory.db, its -wal/-shm and a named V1 store's derived memory_index.db
    # are already covered by the "**/*.db*" rules above.
    "memory_stores/.member-api-key",
    "memory_stores/.member-backups/**",
    "memory_stores/.execution-logs/**",
    "memory_stores/*/backups/**",
]

# JSON leaves whose values are credentials. Stripped from the synced copy and
# restored from the local file on pack, so a token never reaches the backend
# and is never clobbered by another machine's empty value.
SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api_?key|client_?id|access_?key|"
    r"private_?key|credential|bearer|webhook)", re.I)


def _glob_match(posix, pattern):
    """Match *posix* against *pattern* with path-aware wildcards.

    Unlike ``fnmatch``, a single ``*`` or ``?`` never crosses ``/``. ``**``
    matches zero or more path segments (including across separators).
    """
    if pattern == "**":
        return True
    # A leading ** must be peeled off before a trailing one, or a pattern with
    # both -- "**/.git/**" -- matches the endswith branch and is read as a
    # literal directory named "**/.git", which silently denies nothing.
    if pattern.startswith("**/"):
        tail = pattern[3:]
        # Anchor the rest of the pattern at each directory boundary in turn.
        parts = posix.split("/")
        for i in range(len(parts)):
            if _glob_match("/".join(parts[i:]), tail):
                return True
        return False
    if pattern.endswith("/**"):
        root = pattern[:-3]
        return posix == root or posix.startswith(root + "/")

    # Component-wise match so * stays within one segment.
    p_parts = pattern.split("/")
    n_parts = posix.split("/")
    if len(p_parts) != len(n_parts):
        return False
    for pat, name in zip(p_parts, n_parts):
        if not fnmatch.fnmatchcase(name, pat):
            return False
    return True


def _matches(rel_path, patterns):
    posix = rel_path.as_posix()
    for pattern in patterns:
        if _glob_match(posix, pattern):
            return True
    return False


def _allowlisted(rel_path, kirocrew_dir=None):
    """On the allowlist, and (under memory_stores/) inside a usable store."""
    return (_matches(rel_path, ALLOW)
            and stores.file_store_ok(rel_path, kirocrew_dir))


def is_allowed(rel_path, scope=pol.PERSONAL):
    rel_path = Path(rel_path)
    if _matches(rel_path, DENY):
        return False
    if not _allowlisted(rel_path):
        return False
    if scope == pol.TEAM:
        return _matches(rel_path, TEAM_ALLOW)
    return True


def _iter_files(kirocrew_dir):
    """One directory walk: yield (absolute, relative) for every regular file."""
    root = Path(kirocrew_dir)
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        yield path, path.relative_to(root)


def scan_tree(kirocrew_dir, scope=pol.PERSONAL):
    """One walk, three answers: (syncable, vetoed, big_excluded).

    *vetoed* is the allowlisted paths the denylist overrides. The credential
    patterns are deliberately broad, so they can also catch ordinary content --
    e.g. an artifact whose filename contains "secret". Reporting these keeps
    the exclusion from being silent.

    A path held back only because the scope is team counts as neither syncable
    nor vetoed; `withheld_for_scope` reports those separately.
    """
    root = Path(kirocrew_dir)
    syncable, veto, big = [], [], []
    for path, rel in _iter_files(root):
        allow = _allowlisted(rel, root)
        deny = _matches(rel, DENY)
        if allow and deny:
            veto.append(rel)
        elif allow and (scope != pol.TEAM or _matches(rel, TEAM_ALLOW)):
            syncable.append(rel)
        elif not allow and rel.parent == Path(".") \
                and path.stat().st_size > 1024 * 1024:
            big.append((rel, path.stat().st_size))
    return syncable, veto, big


def withheld_for_scope(kirocrew_dir, scope):
    """Allowlisted paths that team scope keeps at home. Empty when personal."""
    if scope != pol.TEAM:
        return []
    return [rel for _, rel in _iter_files(kirocrew_dir)
            if _allowlisted(rel, kirocrew_dir) and not _matches(rel, DENY)
            and not _matches(rel, TEAM_ALLOW)]


def strip_secrets(value, path=""):
    """Return (redacted_copy, {json_path: original_value})."""
    removed = {}
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            here = path + "." + key if path else key
            if SECRET_KEY_RE.search(key) and not isinstance(item, (dict, list)):
                if item not in (None, "", 0, False):
                    removed[here] = item
                continue
            child, child_removed = strip_secrets(item, here)
            out[key] = child
            removed.update(child_removed)
        return out, removed
    if isinstance(value, list):
        out = []
        for index, item in enumerate(value):
            child, child_removed = strip_secrets(item, "%s[%d]" % (path, index))
            out.append(child)
            removed.update(child_removed)
        return out, removed
    return value, removed


def _graft_local_secrets(merged, local):
    """Restore secret-bearing leaves from the local file into a merged one."""
    if not isinstance(merged, dict) or not isinstance(local, dict):
        return merged
    for key, item in local.items():
        if SECRET_KEY_RE.search(key) and not isinstance(item, (dict, list)):
            if key not in merged:
                merged[key] = item
        elif isinstance(item, dict):
            merged[key] = _graft_local_secrets(merged.get(key, {}), item)
    return merged


def unpack_files(kirocrew_dir, out_dir, log=print, scope=pol.PERSONAL):
    """Copy allowlisted files into the repo, normalizing JSON and redacting.

    Returns (written, redacted, vetoed) from a single directory walk; the
    caller reports the vetoed paths rather than walking the tree again.
    """
    root, out = Path(kirocrew_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written, redacted = set(), []
    syncable, veto, _ = scan_tree(root, scope)

    for rel in syncable:
        src, dest = root / rel, out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)

        if src.suffix == ".json":
            try:
                data = json.loads(src.read_text(encoding="utf-8") or "{}")
            except ValueError:
                log("  WARN: %s is not valid JSON; syncing verbatim" % rel)
                shutil.copy2(src, dest)
                written.add(rel)
                continue
            clean, removed = strip_secrets(data)
            if removed:
                redacted.extend("%s:%s" % (rel, k) for k in sorted(removed))
            # Rewritten pretty and key-sorted so git can diff it line by line.
            dest.write_text(dumps_pretty(clean), encoding="utf-8")
        else:
            shutil.copy2(src, dest)
        written.add(rel)

    # Drop anything the allowlist no longer covers or that was deleted locally.
    for path in sorted(out.rglob("*"), reverse=True):
        if path.is_file():
            if path.relative_to(out) not in written:
                path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()

    return written, redacted, veto


def pack_files(in_dir, kirocrew_dir, dry_run=False, log=print,
               scope=pol.PERSONAL):
    """Copy repo files back, re-grafting local secrets into JSON.

    The scope check runs on the way in as well as on the way out. A team repo
    should never contain a personal path, but if one arrives -- from a machine
    running an older build, or a hand-edited repo -- refusing to write it keeps
    a colleague's transcripts off this disk.
    """
    root, src_root = Path(kirocrew_dir), Path(in_dir)
    applied = 0
    if not src_root.exists():
        return applied

    for path in sorted(src_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src_root)
        if not is_allowed(rel, scope):
            log("  WARN: refusing to write out-of-scope path %s" % rel)
            continue
        if not stores.file_store_ok(rel, root):
            # memory_stores/<name> on this machine is a link to somewhere
            # else, possibly another member's store. Never write through it.
            log("  WARN: refusing to write %s: its store directory is a link"
                % rel)
            continue
        dest = root / rel

        if path.suffix == ".json":
            try:
                incoming = json.loads(path.read_text(encoding="utf-8") or "{}")
            except ValueError:
                log("  WARN: %s in repo is not valid JSON; skipped" % rel)
                continue
            local = {}
            if dest.exists():
                try:
                    local = json.loads(dest.read_text(encoding="utf-8") or "{}")
                except ValueError:
                    local = {}
            merged = _graft_local_secrets(incoming, local)
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(dest, dumps_pretty(merged))
        else:
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
        if not dry_run:
            # Upstream keeps memory stores owner-only; so does this copy.
            stores.make_private(root, rel)
        applied += 1
    return applied


def _atomic_write(dest, text):
    tmp = dest.with_suffix(dest.suffix + ".kctmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dest)
