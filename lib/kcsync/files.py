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

from .canon import dumps_pretty

# Only these ever leave the machine.
ALLOW = [
    "config.json",
    "tags.json",
    "tag_boards.json",
    "session_map.json",
    "admission_policy.json",
    "model_windows.json",
    "autonudge.json",
    "hooks.json",
    "sessions/*.jsonl",
    "workspace/*.md",
    "workspace/memory/**",
    "artifacts/**",
]

# Always wins over ALLOW.
DENY = [
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


def _matches(rel_path, patterns):
    posix = rel_path.as_posix()
    for pattern in patterns:
        if fnmatch.fnmatch(posix, pattern):
            return True
        # fnmatch does not treat ** as spanning separators.
        if pattern.endswith("/**") and (
                posix == pattern[:-3] or posix.startswith(pattern[:-2])):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(posix, pattern[3:]):
            return True
        if pattern.startswith("**/") and ("/" + posix).endswith("/" + pattern[3:]):
            return True
    return False


def is_denied(rel_path):
    return _matches(Path(rel_path), DENY)


def is_allowed(rel_path):
    rel_path = Path(rel_path)
    if _matches(rel_path, DENY):
        return False
    return _matches(rel_path, ALLOW)


def iter_syncable(kirocrew_dir):
    """Every file under the allowlist, relative to the KiroCrew directory."""
    root = Path(kirocrew_dir)
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root)
        if is_allowed(rel):
            yield rel


def vetoed(kirocrew_dir):
    """Allowlisted paths the denylist overrides.

    The credential patterns are deliberately broad, so they can also catch
    ordinary content, e.g. an artifact whose filename contains "secret".
    Reporting these keeps the exclusion from being silent.
    """
    root = Path(kirocrew_dir)
    out = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root)
        if _matches(rel, ALLOW) and _matches(rel, DENY):
            out.append(rel)
    return out


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


def unpack_files(kirocrew_dir, out_dir, log=print):
    """Copy allowlisted files into the repo, normalizing JSON and redacting."""
    root, out = Path(kirocrew_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written, redacted = set(), []

    for rel in iter_syncable(root):
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

    return written, redacted


def pack_files(in_dir, kirocrew_dir, dry_run=False, log=print):
    """Copy repo files back, re-grafting local secrets into JSON."""
    root, src_root = Path(kirocrew_dir), Path(in_dir)
    applied = 0
    if not src_root.exists():
        return applied

    for path in sorted(src_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src_root)
        if not is_allowed(rel):
            log("  WARN: refusing to write non-allowlisted path %s" % rel)
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
        applied += 1
    return applied


def _atomic_write(dest, text):
    tmp = dest.with_suffix(dest.suffix + ".kctmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dest)
