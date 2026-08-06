"""Command-line surface. kirocrew-sync.sh drives git and transport; this
module owns everything that touches KiroCrew's data.
"""

import argparse
import os
import sys
from pathlib import Path

from . import FORMAT_VERSION, gates, merge
from . import policy as pol
from .canon import BlobStore
from . import dbio, files


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

    for db_name, rel in pol.DATABASES.items():
        db_path = kirocrew_dir / rel
        if not db_path.exists():
            log("  skip %s (not present)" % rel)
            continue
        out = db_dir / db_name
        policies = dbio.unpack_db(db_path, out, db_name, blobs)
        for stale in dbio.stale_jsonl(out, policies):
            stale.unlink()
        exported = sum(1 for p in policies.values()
                       if p.mode not in (pol.SKIP, pol.LOCAL))
        skipped = [n for n, p in policies.items() if p.mode == pol.LOCAL]
        log("  unpacked %s: %d tables" % (db_name, exported))
        if skipped:
            log("    machine-local, not synced: %s" % ", ".join(sorted(skipped)))

    written, redacted = files.unpack_files(kirocrew_dir, files_dir, log)
    log("  unpacked %d files" % len(written))
    for rel in files.vetoed(kirocrew_dir)[:10]:
        log("    excluded by a credential rule: %s" % rel)
    if redacted:
        log("    redacted %d credential field(s) before sync" % len(redacted))
        for entry in redacted[:10]:
            log("      %s" % entry)

    removed = blobs.sweep()
    if removed:
        log("  pruned %d unreferenced blob(s)" % removed)

    (repo / ".kcsync-format").write_text(str(FORMAT_VERSION) + "\n",
                                         encoding="utf-8")
    return 0


def cmd_pack(args):
    log = _log()
    kirocrew_dir = Path(args.kirocrew_dir)
    repo, db_dir, files_dir, blob_dir = _repo_paths(args.repo)
    blobs = BlobStore(blob_dir)

    findings = gates.run_all(kirocrew_dir, repo)
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

    backups = {}
    if not args.dry_run:
        stamp = _timestamp()
        backup_dir = kirocrew_dir / ".sync" / "backups" / stamp
        for db_name, rel in pol.DATABASES.items():
            db_path = kirocrew_dir / rel
            if db_path.exists():
                backups[db_path] = dbio.backup(db_path, backup_dir / db_name)
        if backups:
            log("  backed up %d database(s) to %s"
                % (len(backups), backup_dir))

    try:
        for db_name, rel in pol.DATABASES.items():
            db_path = kirocrew_dir / rel
            source = db_dir / db_name
            if not db_path.exists() or not source.exists():
                continue
            stats = dbio.pack_db(db_path, source, db_name, blobs,
                                 dry_run=args.dry_run, log=log)
            log("  packed %s: %d rows applied, %d deleted"
                % (db_name, stats["applied"], stats["deleted"]))
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
                log("  restored %s from backup" % db_path.name)
        return 1

    applied = files.pack_files(files_dir, kirocrew_dir,
                               dry_run=args.dry_run, log=log)
    log("  packed %d files" % applied)

    derived = [d for d in pol.DERIVED_DATABASES if (kirocrew_dir / d).exists()]
    if derived:
        log("  note: %s is a derived index; KiroCrew rebuilds it on next run"
            % ", ".join(derived))
    return 0


def cmd_gates(args):
    log = _log()
    findings = gates.run_all(Path(args.kirocrew_dir), Path(args.repo))
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
        Path(args.kirocrew_dir), Path(args.remote), args.label)
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

    for db_name, rel in pol.DATABASES.items():
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

    syncable = list(files.iter_syncable(kirocrew_dir))
    total = sum((kirocrew_dir / r).stat().st_size for r in syncable)
    log("  %d files allowlisted (%.1f KB)" % (len(syncable), total / 1024.0))

    for rel in files.vetoed(kirocrew_dir)[:10]:
        log("  excluded by a credential rule: %s" % rel)

    skipped_big = []
    for path in kirocrew_dir.glob("*"):
        if path.is_file() and path.stat().st_size > 1024 * 1024:
            rel = path.relative_to(kirocrew_dir)
            if not files.is_allowed(rel):
                skipped_big.append((rel, path.stat().st_size))
    for rel, size in sorted(skipped_big, key=lambda x: -x[1])[:5]:
        log("  excluded %s (%.1f MB)" % (rel, size / (1024.0 * 1024.0)))

    return 1 if problems else 0


def _timestamp():
    from datetime import datetime, timezone
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
    p.add_argument("--remote", required=True,
                   help="directory holding the remote ref's db/ tree")
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

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
