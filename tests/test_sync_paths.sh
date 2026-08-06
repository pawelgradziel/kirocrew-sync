#!/usr/bin/env bash
#
# End-to-end test of the sync script's path handling: state unpacked on one
# machine is packed on another with a different $HOME, and the folder source
# has to land on a real directory there.
#
# unpack/pack are driven directly rather than through `sync`, so the test does
# not depend on a storage backend or on whether KiroCrew happens to be running
# on the machine running the tests.
#
# Run: ./tests/test_sync_paths.sh
#

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0

assert_eq() {
    local name="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        printf '  \033[0;32m✓\033[0m %s\n' "$name"
        PASS=$((PASS + 1))
    else
        printf '  \033[0;31m✗\033[0m %s\n      expected: %s\n      actual:   %s\n' \
            "$name" "$expected" "$actual"
        FAIL=$((FAIL + 1))
    fi
}

exists() { [ -e "$1" ] && echo yes || echo no; }

kcsync() { PYTHONPATH="$REPO_DIR/lib" python3 -m kcsync "$@"; }

db_uri() {
    python3 - "$1" <<'PY'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print("\n".join(r[0] for r in db.execute("SELECT uri FROM sources ORDER BY uri")))
db.close()
PY
}

synced_uri() {
    python3 - "$1" <<'PY'
import json, sys
with open(sys.argv[1] + "/db/knowledge/sources.jsonl", encoding="utf-8") as fh:
    print("\n".join(sorted(json.loads(l)["uri"] for l in fh if l.strip())))
PY
}

echo "kirocrew-sync.sh path handling"
echo

# --- two machines, two home directories -------------------------------------
HOME_A="$WORK/homes/alice"
HOME_B="$WORK/homes/bob"
CREW_A="$WORK/machine-a/crew"
CREW_B="$WORK/machine-b/crew"
SYNCED="$WORK/synced"

mkdir -p "$HOME_A/code/groover/docs" "$HOME_B/code/groover/docs"
mkdir -p "$CREW_A/workspace/knowledge" "$CREW_B/workspace/knowledge"

# A knowledge base with one folder source, with the bulk of the data still
# sitting in an uncheckpointed write-ahead log the way a live database would.
make_knowledge_db() {
    python3 - "$1" "${2:-}" <<'PY'
import os, sqlite3, sys
db_path, folder = sys.argv[1], sys.argv[2]
db = sqlite3.connect(db_path)
db.executescript("""
CREATE TABLE sources (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, source_type TEXT NOT NULL,
    uri TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE folder_file_state (
    source_id TEXT NOT NULL, file_path TEXT NOT NULL, last_seen TEXT NOT NULL,
    PRIMARY KEY (source_id, file_path));
""")
db.commit()
db.close()

db = sqlite3.connect(db_path)
db.execute("PRAGMA journal_mode=WAL")
if folder:
    db.execute("INSERT INTO sources VALUES ('docs','Groover Docs','local_folder',?,'now','now')",
               (folder,))
    db.execute("INSERT INTO folder_file_state VALUES ('docs', ?, 'now')",
               (folder + "/readme.md",))
db.commit()
os._exit(0)  # leave the -wal behind
PY
}

make_knowledge_db "$CREW_A/workspace/knowledge/knowledge.db" "$HOME_A/code/groover/docs"
make_knowledge_db "$CREW_B/workspace/knowledge/knowledge.db" ""

assert_eq "machine A starts with an uncheckpointed -wal" \
    "yes" "$(exists "$CREW_A/workspace/knowledge/knowledge.db-wal")"

# --- machine A: unpack ------------------------------------------------------
(
    export HOME="$HOME_A" KIROCREW_PATH_MAP="$HOME_A/no-such-map"
    kcsync unpack --kirocrew-dir "$CREW_A" --repo "$SYNCED"
) > /dev/null 2>&1 || { echo "  unpack step failed" >&2; exit 1; }

# Databases never travel as files now, so no write-ahead log can reach the
# other machine and be replayed onto a database it does not belong to.
assert_eq "no database file is published at all" \
    "" "$(find "$SYNCED" \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' \) -print)"

# WAL-resident rows still make the trip, because unpack reads through SQLite.
assert_eq "the WAL-resident source was exported" \
    "1" "$(grep -c . "$SYNCED/db/knowledge/sources.jsonl")"

assert_eq "the synced form carries a portable path" \
    "~/code/groover/docs" "$(synced_uri "$SYNCED")"

assert_eq "machine A's own database keeps its absolute path" \
    "$HOME_A/code/groover/docs" "$(db_uri "$CREW_A/workspace/knowledge/knowledge.db")"

# --- machine B: pack --------------------------------------------------------
(
    export HOME="$HOME_B" KIROCREW_PATH_MAP="$HOME_B/no-such-map"
    kcsync pack --kirocrew-dir "$CREW_B" --repo "$SYNCED"
) > /dev/null 2>&1 || { echo "  pack step failed" >&2; exit 1; }

assert_eq "the source lands on machine B's own layout" \
    "$HOME_B/code/groover/docs" "$(db_uri "$CREW_B/workspace/knowledge/knowledge.db")"

assert_eq "the decoded path is a real directory on machine B" \
    "yes" "$(exists "$(db_uri "$CREW_B/workspace/knowledge/knowledge.db")")"

assert_eq "per-file state landed on machine B too" \
    "$HOME_B/code/groover/docs/readme.md" \
    "$(python3 -c "
import sqlite3
db = sqlite3.connect('file:$CREW_B/workspace/knowledge/knowledge.db?mode=ro', uri=True)
print(db.execute('SELECT file_path FROM folder_file_state').fetchone()[0])
")"

# pack checkpoints on the way out, so nothing is left stranded in a -wal.
assert_eq "machine B's database is checkpointed after pack" \
    "0" "$(stat -c %s "$CREW_B/workspace/knowledge/knowledge.db-wal" 2>/dev/null || echo 0)"

# --- disabling the feature leaves paths verbatim ----------------------------
rm -rf "$SYNCED"
(
    export HOME="$HOME_A" SYNC_PORTABLE_PATHS=0
    kcsync unpack --kirocrew-dir "$CREW_A" --repo "$SYNCED"
) > /dev/null 2>&1 || { echo "  opt-out step failed" >&2; exit 1; }

assert_eq "SYNC_PORTABLE_PATHS=0 sends the path verbatim" \
    "$HOME_A/code/groover/docs" "$(synced_uri "$SYNCED")"

# --- the running-KiroCrew guard does not trip over this script --------------
#
# The guard greps the process table for "kirocrew", which the sync script's own
# command line contains. Running a copy under a unique name -- and having it
# look for that name instead -- reproduces the self-match on any machine,
# whether or not the real KiroCrew is running here.
# The copy lives beside the original so it still finds config.sh and backends/.
TOOL="$WORK/tool"
cp -r "$REPO_DIR" "$TOOL"
rm -rf "$TOOL/.git"
printf '#!/usr/bin/env bash\n# test config -- KIROCREW_DIR comes from the environment\n' > "$TOOL/config.sh"

FAKE="$TOOL/zzsync-marker.sh"
sed 's/pgrep -f "kirocrew"/pgrep -f "zzsync-marker"/' "$TOOL/kirocrew-sync.sh" > "$FAKE"
printf '\ncheck_kirocrew_running && echo GUARD_CLEAR || echo GUARD_BLOCKED\n' >> "$FAKE"

GUARD_OUT="$(HOME="$HOME_A" KIROCREW_DIR="$CREW_A" SYNC_BACKEND=rsync \
    KIROCREW_SYNC_SKIP_RUNNING_CHECK=0 bash "$FAKE" help 2>&1 | tail -1)"

assert_eq "the guard does not mistake the sync script for KiroCrew" \
    "GUARD_CLEAR" "$GUARD_OUT"

echo
if [ "$FAIL" -eq 0 ]; then
    printf '\033[0;32m✓\033[0m %d passed\n' "$PASS"
    exit 0
fi
printf '\033[0;31m✗\033[0m %d passed, %d failed\n' "$PASS" "$FAIL"
exit 1
