"""Preflight gates.

These catch the failure modes that a merge algorithm cannot: two machines on
different KiroCrew schema versions, two machines on different embedding models,
and credentials about to be uploaded to a storage backend.

Each check returns (level, message) pairs. "error" blocks the sync unless the
matching override flag is passed; "warn" is informational.
"""

import json
from pathlib import Path

from . import policy as pol
from . import stores
from .dbio import connect_ro, read_jsonl, schema_text
from .files import SECRET_KEY_RE

ERROR = "error"
WARN = "warn"
OK = "ok"


def _schema_of(db_path, db_name, scope):
    conn = connect_ro(db_path)
    try:
        return schema_text(conn, db_name, scope)
    finally:
        conn.close()


def check_schema_drift(kirocrew_dir, repo_dir, scope=pol.PERSONAL):
    """Compare the merged repo schema against each live database.

    Scope matters here: team scope exports a subset of the tables, so the
    comparison has to be made against that same subset or the gate fires on
    every sync and users learn to pass --force.
    """
    results = []
    # Every database on this machine, member memory stores included. A store
    # present on only one side has nothing to compare: the machine without it
    # creates it at pack time from the repo's DDL. A store on both sides with
    # a different schema is drift like any other, so the pre-merge check
    # quarantines that machine until the two agree.
    for db_name, rel in stores.databases(kirocrew_dir, scope=scope).items():
        db_path = Path(kirocrew_dir) / rel
        schema_file = Path(repo_dir) / "db" / db_name / "_schema.sql"
        if not db_path.exists() or not schema_file.exists():
            continue
        repo_schema = [l for l in schema_file.read_text(
            encoding="utf-8").splitlines() if l.strip()]
        if any(l.startswith(("<<<<<<<", "=======", ">>>>>>>", "|||||||"))
               for l in repo_schema):
            results.append((ERROR, "%s: schema differs between machines "
                                   "(unresolved conflict in _schema.sql)" % db_name))
            continue
        local_schema = _schema_of(db_path, db_name, scope)
        if sorted(repo_schema) != sorted(local_schema):
            only_repo = set(repo_schema) - set(local_schema)
            only_local = set(local_schema) - set(repo_schema)
            detail = []
            if only_repo:
                detail.append("%d statement(s) only in remote" % len(only_repo))
            if only_local:
                detail.append("%d statement(s) only in local" % len(only_local))
            results.append((ERROR, "%s: schema drift (%s); the machines are on "
                                   "different KiroCrew versions"
                            % (db_name, ", ".join(detail))))
            for statement in sorted(only_repo)[:2]:
                results.append((WARN, "  remote only: %s" % statement[:100]))
            for statement in sorted(only_local)[:2]:
                results.append((WARN, "  local only:  %s" % statement[:100]))
        else:
            results.append((OK, "%s: schema matches" % db_name))
    return results


def _repo_memory_meta(repo_dir, db_name="memory"):
    path = Path(repo_dir) / "db" / db_name / "memory_meta.jsonl"
    if not path.exists():
        return {}
    try:
        return {r.get("key"): r.get("value") for r in read_jsonl(path)}
    except ValueError:
        return {}


def local_embedding_sig(kirocrew_dir):
    """This machine's embedding_space_sig, read one way for every caller that
    needs to compare it: the pre-merge gate below, and the export/import seed
    primitive in cli.py, which checks it against a manifest before anything
    is materialized. One place that knows memory_meta's key/value shape.
    """
    return _db_embedding_sig(Path(kirocrew_dir) / pol.DATABASES["memory"])


def _db_embedding_sig(db_path):
    """embedding_space_sig from one memory-shaped database's memory_meta."""
    if not db_path.exists():
        return None

    conn = connect_ro(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM memory_meta WHERE key='embedding_space_sig'"
        ).fetchone()
        return row["value"] if row else None
    except Exception:
        return None
    finally:
        conn.close()


def check_embedding_space(kirocrew_dir, repo_dir, scope=pol.PERSONAL):
    """Refuse to mix vectors produced by different embedding models.

    Nothing downstream validates this: mismatched vectors in one index degrade
    semantic search silently, with no error and no visible corruption.
    """
    results = []
    local_sig = local_embedding_sig(kirocrew_dir)
    remote_sig = _repo_memory_meta(repo_dir).get("embedding_space_sig")
    if local_sig and remote_sig and local_sig != remote_sig:
        results.append((ERROR,
                        "embedding space mismatch (local %s, remote %s); "
                        "merging would mix incompatible vectors"
                        % (local_sig[:12], remote_sig[:12])))
    elif local_sig and remote_sig:
        results.append((OK, "embedding space matches (%s)" % local_sig[:12]))
    if scope == pol.PERSONAL:
        results.extend(_check_store_embedding_spaces(kirocrew_dir, repo_dir,
                                                     local_sig))
    return results


def _check_store_embedding_spaces(kirocrew_dir, repo_dir, local_global_sig):
    """The same check for each member memory store in the remote tree.

    Each store records its own embedding_space_sig (upstream reconciles it
    per file). A store on both machines with two different signatures is the
    real hazard -- a merge would put both models' vectors in one file -- so
    that is an ERROR and the remote machine is quarantined.

    A store that exists only on the remote is compared with this machine's
    global signature instead, and only warned about: it arrives as its own
    file, nothing is mixed into an existing one, and KiroCrew reconciles a
    store's embedding space when it opens it. Refusing would quarantine a
    whole machine over one idle store that has not been re-embedded yet.
    """
    results = []
    local = set(stores.local_stores(kirocrew_dir))
    for name in stores.repo_stores(repo_dir):
        db_name = stores.db_name(name)
        remote_sig = _repo_memory_meta(repo_dir, db_name).get(
            "embedding_space_sig")
        if not remote_sig:
            continue
        if name in local:
            local_sig = _db_embedding_sig(
                Path(kirocrew_dir) / stores.rel_path(name))
            if local_sig and local_sig != remote_sig:
                results.append((ERROR,
                    "%s: embedding space mismatch (local %s, remote %s); "
                    "merging would mix incompatible vectors"
                    % (db_name, local_sig[:12], remote_sig[:12])))
        elif local_global_sig and local_global_sig != remote_sig:
            results.append((WARN,
                "%s: arrives embedded with %s, this machine uses %s; "
                "KiroCrew reconciles its embedding space when it opens the store"
                % (db_name, remote_sig[:12], local_global_sig[:12])))
    return results


def check_item_embedding_sigs(repo_dir):
    """Warn if merged knowledge items carry more than one embedding signature."""
    path = Path(repo_dir) / "db" / "knowledge" / "items.jsonl"
    if not path.exists():
        return []
    try:
        rows = read_jsonl(path)
    except ValueError:
        return []
    sigs = {r.get("embedding_sig") for r in rows if r.get("embedding_sig")}
    if len(sigs) > 1:
        return [(WARN, "knowledge items span %d embedding signatures; "
                       "re-embed for consistent search results" % len(sigs))]
    return []


def check_no_secrets(repo_dir):
    """Backstop scan of what is about to be pushed."""
    results = []
    files_dir = Path(repo_dir) / "files"
    if not files_dir.exists():
        return results
    for path in sorted(files_dir.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError:
            continue
        hits = sorted(_find_secret_leaves(data))
        if hits:
            results.append((ERROR, "%s still contains credential-shaped values: %s"
                            % (path.relative_to(repo_dir), ", ".join(hits[:5]))))
    return results


def _find_secret_leaves(value, path=""):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = path + "." + key if path else key
            if SECRET_KEY_RE.search(key) and not isinstance(item, (dict, list)):
                if item not in (None, "", 0, False):
                    found.append(here)
            else:
                found.extend(_find_secret_leaves(item, here))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_find_secret_leaves(item, "%s[%d]" % (path, index)))
    return found


def run_all(kirocrew_dir, repo_dir, scope=pol.PERSONAL):
    results = []
    results.extend(check_schema_drift(kirocrew_dir, repo_dir, scope))
    results.extend(check_embedding_space(kirocrew_dir, repo_dir, scope))
    results.extend(check_item_embedding_sigs(repo_dir))
    results.extend(check_no_secrets(repo_dir))
    return results


def check_scope(local_scope, remote_scope, label):
    """Refuse to merge a machine syncing at a different scope.

    A personal repo carries transcripts and per-person config that a team repo
    deliberately excludes. Merging the two would push exactly the data team
    scope exists to hold back, so the mismatch is treated like any other
    incompatibility: that machine is quarantined, everyone else carries on.
    """
    if local_scope == remote_scope:
        return []
    return [(ERROR, "scope mismatch (local %s, remote %s); %s is syncing a "
                    "different set of data" % (local_scope, remote_scope, label))]


def check_compatibility(kirocrew_dir, remote_dir, label, scope=pol.PERSONAL):
    """Compare a remote machine's state against local, before merging.

    These two checks have to run pre-merge: the merge collapses each row to a
    single winner, so afterwards there is nothing left to compare. Schema and
    embedding space are exactly the properties that must agree for a merge to
    mean anything.
    """
    results = []
    for level, message in check_schema_drift(kirocrew_dir, remote_dir, scope):
        if level != OK:
            results.append((level, "%s: %s" % (label, message)))
    for level, message in check_embedding_space(kirocrew_dir, remote_dir, scope):
        if level != OK:
            results.append((level, "%s: %s" % (label, message)))
    return results
