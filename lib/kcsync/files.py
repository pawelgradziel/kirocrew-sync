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
from .canon import dumps_pretty

# Only these ever leave the machine.
# Glob rules (path-aware, not raw fnmatch): * and ? never cross '/', ** spans
# directories. So "sessions/*.jsonl" is top-level only; use ** for recursion.
ALLOW = [
    "config.json",
    # The two one-shot ledgers KiroCrew keeps beside config.json. Each records
    # "this config document already went through a migration", and the load
    # path consults them before deleting keys (upstream config/loader.py
    # load(): strips a stored `connections_ui: false` unless
    # connections_ui_migrated.json exists; config/superseded_defaults.py
    # auto_adoptable()/record_adoptions(): removes a stored old default unless
    # superseded_acked.json lists it as adopted or acknowledged). Left behind,
    # a machine that never saw the ledger deleted a value the other machine's
    # operator had deliberately kept, and the merge carried the deletion back
    # to that machine too. They describe the synced document, so they travel
    # with it.
    "connections_ui_migrated.json",
    "superseded_acked.json",
    "tags.json",
    "tag_boards.json",
    "admission_policy.json",
    "model_windows.json",
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
#
# autonudge.json is out for the crons.json reason (see above) and the
# session_map.json reason together. It holds live self-prompting loops bound
# to chat slots, and every gateway re-arms the loops it finds at startup
# (upstream autonudge.py), so a synced copy made every machine fire the same
# nudges into its own copy of the conversation. And AutoNudgeService._load() rewrites the
# store from host state: rows whose addressing fields fail this host's
# credential policy move into autonudge.quarantine.json (never synced), a loop
# interrupted mid-delivery is stopped, and repair_sentinel_path() re-homes or
# drops stop_sentinel_path against the local data home. "loops" is one list,
# which merge_json treats as a single value, so that rewrite beat the untouched
# copy on the machine that owned the loops.
DENY = [
    "session_map.json",
    "autonudge.json",
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
]

# JSON leaves whose values are credentials. Stripped from the synced copy and
# restored from the local file on pack, so a token never reaches the backend
# and is never clobbered by another machine's empty value.
SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api_?key|client_?id|access_?key|"
    r"private_?key|credential|bearer|webhook)", re.I)

# JSON leaves that describe this machine rather than the user's settings.
# Handled like the secrets above: left out of the synced copy, and on pack the
# local value (or its absence) is put back, whatever the merged copy says.
#
# memory.embed_model_stamp is (st_dev, st_ino, size, mtime_ns, ctime_ns) of the
# local custom embedding model file, and embed_model_legacy_ids names the
# vector space this machine's own vectors were built in before the file was
# recorded. Upstream embeddings._verify_custom_model() rewrites the stamp when
# it does not match the local file, and can pop the legacy ids. Synced, each
# machine re-hashed the model weights after every sync and wrote its own stamp
# back. Worse, on a machine whose vectors predate the recorded model file, the
# reconcile in embeddings.py accepts those vectors only while the stored stamp
# matches the local file; another machine's stamp fails that check and starts
# an embedding-space change, a full re-embed on a machine that changed nothing.
LOCAL_ONLY_KEYS = {
    "config.json": ("memory.embed_model_stamp", "memory.embed_model_legacy_ids"),
}


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


def is_allowed(rel_path, scope=pol.PERSONAL):
    rel_path = Path(rel_path)
    if _matches(rel_path, DENY):
        return False
    if not _matches(rel_path, ALLOW):
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
        allow = _matches(rel, ALLOW)
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
            if _matches(rel, ALLOW) and not _matches(rel, DENY)
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


def _parent_of(value, dotted):
    """(dict holding the leaf, leaf name) for *dotted* in *value*, or (None, leaf)."""
    *parents, leaf = dotted.split(".")
    node = value
    for part in parents:
        node = node.get(part) if isinstance(node, dict) else None
    return (node if isinstance(node, dict) else None), leaf


def strip_local_only(rel_path, value):
    """Drop this file's LOCAL_ONLY_KEYS from *value* in place."""
    for dotted in LOCAL_ONLY_KEYS.get(Path(rel_path).as_posix(), ()):
        parent, leaf = _parent_of(value, dotted)
        if parent is not None:
            parent.pop(leaf, None)
    return value


def _graft_local_only(rel_path, merged, local):
    """Make each LOCAL_ONLY_KEYS leaf in *merged* match the local file.

    The local value wins, and a leaf the local file lacks is removed, so a
    repo written by a build that still synced these keys cannot plant another
    machine's value here. A parent the merged copy no longer has is not
    recreated: the section it belonged to was deleted on purpose.
    """
    for dotted in LOCAL_ONLY_KEYS.get(Path(rel_path).as_posix(), ()):
        dest, leaf = _parent_of(merged, dotted)
        if dest is None:
            continue
        src, _ = _parent_of(local, dotted)
        if src is not None and leaf in src:
            dest[leaf] = src[leaf]
        else:
            dest.pop(leaf, None)
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
            strip_local_only(rel, clean)
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
            merged = _graft_local_only(rel, merged, local)
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(dest, dumps_pretty(merged))
        else:
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
        applied += 1
    return applied


def _atomic_write(dest, text):
    tmp = dest.with_suffix(dest.suffix + ".kctmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dest)
