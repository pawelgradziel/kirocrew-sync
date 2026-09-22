"""Command-line surface. kirocrew-sync.sh drives git and transport; this
module owns everything that touches KiroCrew's data.
"""

import argparse
import collections
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import FORMAT_VERSION, gates, merge
from . import paths as pathmod
from . import policy as pol
from .canon import BlobStore
from . import dbio, files, stores
from . import mailbox


def _repo_paths(repo):
    repo = Path(repo)
    return repo, repo / "db", repo / "files", repo / "blob"


def _log(prefix=""):
    def emit(message):
        sys.stderr.write(prefix + str(message) + "\n")
    return emit


# --------------------------------------------------------------------------

def cmd_unpack(args):
    log = _log()
    kirocrew_dir = Path(args.kirocrew_dir)
    repo, db_dir, files_dir, blob_dir = _repo_paths(args.repo)
    repo.mkdir(parents=True, exist_ok=True)
    blobs = BlobStore(blob_dir)

    for db_name, rel in stores.databases(kirocrew_dir, scope=args.scope,
                                         log=log).items():
        db_path = kirocrew_dir / rel
        if not db_path.exists():
            log("  skip %s (not present)" % rel)
            continue
        out = db_dir / db_name
        policies = dbio.unpack_db(db_path, out, db_name, blobs, args.scope)
        for stale in dbio.stale_jsonl(out, policies, args.scope):
            stale.unlink()
        if pol.is_store_db(db_name):
            # What a machine without this store needs to create it (pack).
            stores.write_ddl(db_path, out)
        exported = sum(1 for p in policies.values()
                       if pol.is_exported(p, args.scope))
        skipped = [n for n, p in policies.items() if p.mode == pol.LOCAL]
        held = [n for n, p in policies.items()
                if pol.is_exported(p) and not pol.is_exported(p, args.scope)]
        log("  unpacked %s: %d tables" % (db_name, exported))
        if skipped:
            log("    machine-local, not synced: %s" % ", ".join(sorted(skipped)))
        if held:
            log("    personal, held back from the team: %s" % ", ".join(sorted(held)))

    if args.scope != pol.PERSONAL:
        # Member memory is private and never discovered in team scope. A
        # store tree in a team repo can only come from a bug or a hand edit;
        # removing it here keeps it out of the next publish.
        leaked = db_dir / stores.STORES_DIR
        if leaked.exists():
            shutil.rmtree(leaked)
            log("  removed member memory stores from the team repo")
        held_stores = stores.local_stores(kirocrew_dir)
        if held_stores:
            log("  %d member memory store(s) private, held back from the team"
                % len(held_stores))

    written, redacted, veto = files.unpack_files(kirocrew_dir, files_dir, log,
                                                 args.scope)
    log("  unpacked %d files" % len(written))
    for rel in veto[:10]:
        log("    excluded by a credential rule: %s" % rel)
    if redacted:
        log("    redacted %d credential field(s) before sync" % len(redacted))
        for entry in redacted[:10]:
            log("      %s" % entry)

    held_files = files.withheld_for_scope(kirocrew_dir, args.scope)
    if held_files:
        log("  %d file(s) personal, held back from the team" % len(held_files))
        for rel in held_files[:5]:
            log("      %s" % rel)

    removed = blobs.sweep()
    if removed:
        log("  pruned %d unreferenced blob(s)" % removed)

    (repo / ".kcsync-format").write_text(str(FORMAT_VERSION) + "\n",
                                         encoding="utf-8")
    # Read by the remote-compatibility gate: merging a personal repo into a
    # team one would publish exactly what team scope holds back.
    (repo / ".kcsync-scope").write_text(args.scope + "\n", encoding="utf-8")
    return 0


def cmd_pack(args):
    log = _log()
    kirocrew_dir = Path(args.kirocrew_dir)
    repo, db_dir, files_dir, blob_dir = _repo_paths(args.repo)
    blobs = BlobStore(blob_dir)

    findings = gates.run_all(kirocrew_dir, repo, args.scope)
    blocking = [m for level, m in findings if level == gates.ERROR]
    for level, message in findings:
        if level == gates.ERROR:
            log("  ERROR: " + message)
        elif level == gates.WARN:
            log("  WARN:  " + message)
        else:
            log("  ok:    " + message)
    if blocking and not args.force:
        log("")
        log("  Refusing to pack. Re-run with --force to override.")
        return 2

    # Local stores plus any that arrived from another machine. Team scope
    # yields the fixed databases only, so a store tree in a team repo is
    # never packed; say so rather than skip it silently.
    databases = stores.databases(kirocrew_dir, repo_dir=repo, scope=args.scope,
                                 log=log)
    if args.scope != pol.PERSONAL and (db_dir / stores.STORES_DIR).exists():
        log("  WARN: refusing to pack member memory stores in team scope")

    backups = {}
    if not args.dry_run:
        stamp = _timestamp()
        backup_dir = kirocrew_dir / ".sync" / "backups" / stamp
        for db_name, rel in databases.items():
            db_path = kirocrew_dir / rel
            if db_path.exists():
                backups[db_path] = dbio.backup(db_path, backup_dir / db_name)
        if backups:
            log("  backed up %d database(s) to %s"
                % (len(backups), backup_dir))

    created = []
    try:
        for db_name, rel in databases.items():
            db_path = kirocrew_dir / rel
            source = db_dir / db_name
            if not source.exists():
                continue
            store = stores.store_of(db_name)
            if store is not None and not stores.writable_here(kirocrew_dir,
                                                              store):
                # e.g. memory_stores/<name> here is a link to another store:
                # packing through it would merge two members' memory.
                log("  WARN: not packing memory store %s: its directory or "
                    "database is a link" % store)
                continue
            if not db_path.exists():
                # Only a member store is created here. memory.db and
                # knowledge.db belong to KiroCrew's own first run.
                if store is None:
                    continue
                if not (source / stores.DDL_FILE).exists():
                    log("  WARN: memory store %s has no %s in the repo; "
                        "not created" % (store, stores.DDL_FILE))
                    continue
                if args.dry_run:
                    log("  would create memory store %s" % store)
                    continue
                db_path = stores.create_from_repo(kirocrew_dir, store, source)
                created.append(db_path)
                log("  created memory store %s from the sync repo" % store)
            stats = dbio.pack_db(db_path, source, db_name, blobs,
                                 dry_run=args.dry_run, log=log,
                                 scope=args.scope)
            log("  packed %s: %d rows written, %d deleted"
                % (db_name, stats["applied"], stats["deleted"]))
            if store is not None and not args.dry_run:
                count = stores.rebuild_member_fts(db_path, log)
                if count is not None:
                    log("  rebuilt memory_fts for %s: %d entries"
                        % (store, count))
            if stats["orphans"]:
                log("  WARN: %d orphaned row(s) in %s after merge"
                    % (len(stats["orphans"]), db_name))
                for orphan in stats["orphans"][:5]:
                    log("      %s -> missing %s"
                        % (orphan["table"], orphan["parent"]))
            for warning in stats.get("path_warnings", [])[:5]:
                log("  WARN: %s" % warning)
            if stats.get("path_warnings"):
                log("        run './kirocrew-sync.sh paths' for the full picture")
    except Exception as exc:
        log("  FAILED: %s" % exc)
        for db_path, backup_path in backups.items():
            if backup_path:
                dbio.restore(backup_path, db_path)
                log("  restored %s from backup"
                    % db_path.relative_to(kirocrew_dir))
        # A store this pack created had nothing to restore: remove it, so the
        # next sync creates it again from a clean start.
        for db_path in created:
            stores.remove_created(db_path)
            log("  removed partially created %s"
                % db_path.relative_to(kirocrew_dir))
        return 1

    applied = files.pack_files(files_dir, kirocrew_dir,
                               dry_run=args.dry_run, log=log,
                               scope=args.scope)
    log("  packed %d files" % applied)

    derived = [d for d in pol.DERIVED_DATABASES if (kirocrew_dir / d).exists()]
    if derived:
        log("  note: %s is a derived index; KiroCrew rebuilds it on next run"
            % ", ".join(derived))
    return 0


def cmd_gates(args):
    log = _log()
    findings = gates.run_all(Path(args.kirocrew_dir), Path(args.repo),
                             args.scope)
    worst = 0
    for level, message in findings:
        if level == gates.ERROR:
            log("  ERROR: " + message)
            worst = 2
        elif level == gates.WARN:
            log("  WARN:  " + message)
            worst = max(worst, 1)
        else:
            log("  ok:    " + message)
    if not findings:
        log("  nothing to check yet")
    return worst if args.strict else 0


def cmd_compat(args):
    """Pre-merge compatibility check against one remote machine's tree."""
    log = _log()
    findings = gates.check_compatibility(
        Path(args.kirocrew_dir), Path(args.remote), args.label, args.scope)
    findings += gates.check_scope(args.scope, args.remote_scope, args.label)
    for level, message in findings:
        log("  %s: %s" % ("ERROR" if level == gates.ERROR else "WARN", message))
    return 2 if any(level == gates.ERROR for level, _ in findings) else 0


def cmd_merge_driver(args):
    if args.kind == "rows":
        return merge.driver_rows([args.ancestor, args.ours, args.theirs, args.path])
    return merge.driver_json([args.ancestor, args.ours, args.theirs, args.path])


def cmd_doctor(args):
    log = _log()
    kirocrew_dir = Path(args.kirocrew_dir)
    problems = 0

    log("KiroCrew directory: %s" % kirocrew_dir)
    if not kirocrew_dir.exists():
        log("  ERROR: directory does not exist")
        return 2

    for db_name, rel in stores.databases(kirocrew_dir, scope=args.scope,
                                         log=log).items():
        db_path = kirocrew_dir / rel
        if not db_path.exists():
            log("  %-10s missing (%s)" % (db_name, rel))
            continue
        main = db_path.stat().st_size
        wal_path = Path(str(db_path) + "-wal")
        wal = wal_path.stat().st_size if wal_path.exists() else 0
        note = ""
        if wal > main:
            note = "  <- most data is in the WAL; a file copy would lose it"
            problems += 1
        log("  %-10s %8d B db + %8d B wal%s" % (db_name, main, wal, note))

    for name in pol.DERIVED_DATABASES:
        if (kirocrew_dir / name).exists():
            log("  %-10s derived index, excluded from sync" % name)

    syncable, veto, skipped_big = files.scan_tree(kirocrew_dir, args.scope)
    total = sum((kirocrew_dir / r).stat().st_size for r in syncable)
    log("  %d files allowlisted (%.1f KB)" % (len(syncable), total / 1024.0))

    for rel in veto[:10]:
        log("  excluded by a credential rule: %s" % rel)

    for rel, size in sorted(skipped_big, key=lambda x: -x[1])[:5]:
        log("  excluded %s (%.1f MB)" % (rel, size / (1024.0 * 1024.0)))

    return 1 if problems else 0


def cmd_conflicts(args):
    """Summarize an automatic-resolution conflict log (JSONL)."""
    log_path = Path(args.log)
    if not log_path.is_file() or log_path.stat().st_size == 0:
        return 0
    counts = collections.Counter()
    examples = {}
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            key = (entry.get("table"), entry.get("kind"), entry.get("resolution"))
            counts[key] += 1
            examples.setdefault(key, entry.get("key", ""))
    for (table, kind, resolution), count in counts.most_common(10):
        sample = str(examples[(table, kind, resolution)])[:60]
        print("    %-24s %-14s %-12s x%d  e.g. %s"
              % (table, kind, resolution, count, sample))
    return 0


def cmd_paths(args):
    """Report knowledge source path health.

    Exit codes are three-way so the caller can tell "all good" from "nothing
    to check": 0 all paths resolve, 1 some need attention, 2 no database.
    """
    pp = pathmod.portable_paths
    if pp is None:
        sys.stderr.write("  path translation is unavailable on this machine\n")
        return 2

    db = Path(args.kirocrew_dir) / pol.DATABASES["knowledge"]
    if not db.is_file():
        sys.stderr.write("  no knowledge database at %s\n" % db)
        return 2

    map_file = os.environ.get("KIROCREW_PATH_MAP")
    try:
        mappings = pp.load_mappings(map_file)
    except (pp.PathMapError, OSError) as exc:
        sys.stderr.write("  path map unusable: %s\n" % exc)
        return 1
    return pp.report(db, mappings)


def cmd_seed_manifest(args):
    """Build the manifest that travels inside an export archive.

    Printed to stdout as the only output (diagnostics, if any, go through
    _log() to stderr as usual) so the caller in kirocrew-sync.sh can redirect
    it straight into the archive's manifest.json.
    """
    manifest = {
        "scope": args.scope,
        "machine_id": args.machine_id,
        "format_version": FORMAT_VERSION,
        "embedding_space_sig": gates.local_embedding_sig(args.kirocrew_dir),
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def cmd_seed_check(args):
    """Validate an import archive's manifest against local state.

    Runs before anything is materialized -- there is no repo or ref to run
    the ordinary compat gate against yet, so this compares the manifest's
    flat fields directly rather than reading a fetched tree. Same two
    properties as sync's pre-merge gates (check_scope, check_embedding_space)
    and the same rationale: scope decides what a machine is allowed to hold,
    and mixed embedding spaces degrade search silently.
    """
    log = _log()
    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log("  ERROR: unreadable manifest: %s" % exc)
        return 2

    findings = list(gates.check_scope(
        args.scope, manifest.get("scope", pol.PERSONAL), "the archive"))

    remote_sig = manifest.get("embedding_space_sig")
    local_sig = gates.local_embedding_sig(args.kirocrew_dir)
    if local_sig and remote_sig and local_sig != remote_sig:
        findings.append((gates.ERROR,
            "embedding space mismatch (local %s, archive %s); importing "
            "would mix incompatible vectors"
            % (local_sig[:12], remote_sig[:12])))

    for level, message in findings:
        log("  %s: %s" % ("ERROR" if level == gates.ERROR else "WARN", message))
    if not findings:
        log("  ok: scope and embedding space match")
    return 2 if any(level == gates.ERROR for level, _ in findings) else 0



# Structural/identity rows, not accumulated content: schema_version and
# memory_meta record the schema version and the embedding model, not
# anything a person added, and both are themselves synced tables (the
# compat gates depend on comparing them). Every machine that has ever run
# KiroCrew has them populated, so counting them here would make the
# fresh-machine branch of import's existing-state gate unreachable on any
# machine with a real install -- the exact case the gate exists to let
# through without --force.
#
# member_database is the same kind of row for a member memory store: its
# (member_id, store_id) identity, written when the store is created.
_BOOKKEEPING_TABLES = frozenset(["schema_version", "memory_meta",
                                 "member_database"])


def cmd_seed_has_data(args):
    """Predicate for import's existing-state gate.

    Exit 0 (and print a short summary of what was found) if this scope
    already has synced database rows or allowlisted files that --force would
    discard; exit 1 if there is nothing here yet, meaning a fresh machine is
    safe to seed without --force.
    """
    kirocrew_dir = Path(args.kirocrew_dir)
    found = False
    for db_name, rel in stores.databases(kirocrew_dir,
                                         scope=args.scope).items():
        db_path = kirocrew_dir / rel
        if not db_path.exists():
            continue
        counts = dbio.exported_row_counts(db_path, db_name, args.scope)
        counts = {name: n for name, n in counts.items()
                  if name not in _BOOKKEEPING_TABLES}
        total = sum(counts.values())
        if total:
            found = True
            tables_with_rows = sum(1 for c in counts.values() if c)
            print("  %s: %d synced row(s) across %d table(s)"
                  % (rel, total, tables_with_rows))
    syncable, _veto, _big = files.scan_tree(kirocrew_dir, args.scope)
    if syncable:
        found = True
        print("  %d synced file(s) (config.json, sessions, artifacts, ...)"
              % len(syncable))
    return 0 if found else 1


def _timestamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="kcsync", description="KiroCrew sync data engine")
    default_dir = os.environ.get("KIROCREW_DIR",
                                 os.path.expanduser("~/.kiro/crew"))
    sub = parser.add_subparsers(dest="command", required=True)

    def with_common(p, need_repo=True):
        p.add_argument("--kirocrew-dir", default=default_dir)
        p.add_argument("--scope", choices=list(pol.SCOPES), default=pol.PERSONAL,
                       help="personal (default) syncs everything syncable; "
                            "team syncs only what is marked shared")
        if need_repo:
            p.add_argument("--repo", required=True)
        return p

    p = with_common(sub.add_parser("unpack", help="databases+files -> repo"))
    p.set_defaults(func=cmd_unpack)

    p = with_common(sub.add_parser("pack", help="repo -> databases+files"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="pack even if a preflight gate fails")
    p.set_defaults(func=cmd_pack)

    p = with_common(sub.add_parser("gates", help="run preflight checks"))
    p.add_argument("--strict", action="store_true",
                   help="exit non-zero when a check fails")
    p.set_defaults(func=cmd_gates)

    p = sub.add_parser("compat", help="check a remote tree against local")
    p.add_argument("--kirocrew-dir", default=default_dir)
    p.add_argument("--scope", choices=list(pol.SCOPES), default=pol.PERSONAL)
    p.add_argument("--remote", required=True,
                   help="directory holding the remote ref's db/ tree")
    p.add_argument("--remote-scope", default=pol.PERSONAL,
                   help="scope the remote machine published at")
    p.add_argument("--label", default="remote")
    p.set_defaults(func=cmd_compat)

    p = sub.add_parser("merge-driver", help="git merge driver entry point")
    p.add_argument("kind", choices=["rows", "json"])
    p.add_argument("ancestor")
    p.add_argument("ours")
    p.add_argument("theirs")
    p.add_argument("path")
    p.set_defaults(func=cmd_merge_driver)

    p = with_common(sub.add_parser("doctor", help="inspect local state"),
                    need_repo=False)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("conflicts",
                       help="summarize an automatic conflict log (JSONL)")
    p.add_argument("log", help="path to conflicts.jsonl")
    p.set_defaults(func=cmd_conflicts)

    p = with_common(sub.add_parser(
        "paths", help="check knowledge source path portability"),
                    need_repo=False)
    p.set_defaults(func=cmd_paths)

    p = with_common(sub.add_parser(
        "seed-manifest", help="build the manifest for an export archive"),
                    need_repo=False)
    p.add_argument("--machine-id", required=True)
    p.set_defaults(func=cmd_seed_manifest)

    p = with_common(sub.add_parser(
        "seed-check", help="validate an import archive's manifest against local state"),
                    need_repo=False)
    p.add_argument("--manifest", required=True)
    p.set_defaults(func=cmd_seed_check)

    p = with_common(sub.add_parser(
        "seed-has-data",
        help="exit 0 if this scope already has synced state (import's existing-state gate)"),
                    need_repo=False)
    p.set_defaults(func=cmd_seed_has_data)

    mailbox.add_parsers(sub, default_dir)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
