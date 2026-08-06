#!/usr/bin/env bash
#
# End-to-end test of the sync script's path handling: a bundle built on one
# machine is applied on another with a different $HOME, and the folder source
# has to land on a real directory there.
#
# The bundle/apply functions are sourced and called directly rather than going
# through `push`/`pull`, so the test does not depend on whether KiroCrew
# happens to be running on the machine running the tests.
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

db_uri() {
    python3 - "$1" <<'PY'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print("\n".join(r[0] for r in db.execute("SELECT uri FROM sources ORDER BY uri")))
db.close()
PY
}

if ! command -v rsync &> /dev/null; then
    echo "rsync is required for these tests" >&2
    exit 1
fi

echo "kirocrew-sync.sh path handling"
echo

# --- a copy of the tool, so the test never writes to the real config --------
TOOL="$WORK/tool"
cp -r "$REPO_DIR" "$TOOL"
rm -rf "$TOOL/.git"
printf '#!/usr/bin/env bash\n# test config -- KIROCREW_DIR comes from the environment\n' > "$TOOL/config.sh"

# --- two machines, two home directories -------------------------------------
HOME_A="$WORK/homes/alice"
HOME_B="$WORK/homes/bob"
CREW_A="$WORK/machine-a/crew"
CREW_B="$WORK/machine-b/crew"
BUNDLE="$WORK/bundle"

mkdir -p "$HOME_A/code/groover/docs" "$HOME_B/code/groover/docs"
mkdir -p "$CREW_A/workspace/knowledge" "$CREW_B/workspace/knowledge"

# Machine A's knowledge base: one folder source under alice's home, with the
# bulk of the data still sitting in an uncheckpointed write-ahead log.
python3 - "$CREW_A/workspace/knowledge/knowledge.db" "$HOME_A/code/groover/docs" <<'PY'
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
db.execute("INSERT INTO sources VALUES ('docs', 'Groover Docs', 'local_folder', ?, 'now', 'now')", (folder,))
db.execute("INSERT INTO folder_file_state VALUES ('docs', ?, 'now')", (folder + "/readme.md",))
db.commit()
os._exit(0)  # leave the -wal behind, the way a live database would
PY

# Machine A's memory database is checkpointed: it travels as a single file.
python3 -c "
import sqlite3
db = sqlite3.connect('$CREW_A/memory.db')
db.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY)')
db.commit(); db.close()"

# Machine B has an older sync's leftovers, including a write-ahead log with no
# counterpart in the bundle. Replaying it onto the incoming database would
# corrupt it, so applying the bundle has to drop it.
cp "$CREW_A/memory.db" "$CREW_B/memory.db"
printf 'stale wal contents' > "$CREW_B/memory.db-wal"
printf 'stale shm contents' > "$CREW_B/memory.db-shm"

assert_eq "machine A starts with an uncheckpointed -wal" \
    "yes" "$(exists "$CREW_A/workspace/knowledge/knowledge.db-wal")"

# --- machine A: build the bundle --------------------------------------------
(
    export HOME="$HOME_A" KIROCREW_DIR="$CREW_A" SYNC_BACKEND=rsync
    # shellcheck source=/dev/null
    source "$TOOL/kirocrew-sync.sh" help > /dev/null
    set +e
    prepare_sync_bundle "$BUNDLE" > /dev/null
    translate_bundle_paths "$BUNDLE" encode > /dev/null
) || { echo "  bundle step failed" >&2; exit 1; }

# Checked before anything reads the database: opening a WAL-mode database, even
# read-only, recreates the -wal file.
assert_eq "the bundle's -wal is folded into the database" \
    "no" "$(exists "$BUNDLE/workspace/knowledge/knowledge.db-wal")"

assert_eq "the bundle carries a portable path" \
    "~/code/groover/docs" "$(db_uri "$BUNDLE/workspace/knowledge/knowledge.db")"

assert_eq "machine A's own database keeps its absolute path" \
    "$HOME_A/code/groover/docs" "$(db_uri "$CREW_A/workspace/knowledge/knowledge.db")"

# --- machine B: apply the bundle --------------------------------------------
(
    export HOME="$HOME_B" KIROCREW_DIR="$CREW_B" SYNC_BACKEND=rsync
    # shellcheck source=/dev/null
    source "$TOOL/kirocrew-sync.sh" help > /dev/null
    set +e
    translate_bundle_paths "$BUNDLE" decode > /dev/null
    apply_sync_bundle "$BUNDLE" > /dev/null
) || { echo "  apply step failed" >&2; exit 1; }

assert_eq "the source lands on machine B's own layout" \
    "$HOME_B/code/groover/docs" "$(db_uri "$CREW_B/workspace/knowledge/knowledge.db")"

assert_eq "the decoded path is a real directory on machine B" \
    "yes" "$(exists "$(db_uri "$CREW_B/workspace/knowledge/knowledge.db")")"

assert_eq "per-file state landed on machine B too" \
    "$HOME_B/code/groover/docs/readme.md" \
    "$(python3 -c "
import sqlite3, sys
db = sqlite3.connect('file:$CREW_B/workspace/knowledge/knowledge.db?mode=ro', uri=True)
print(db.execute('SELECT file_path FROM folder_file_state').fetchone()[0])
")"

assert_eq "machine B's stale memory.db-wal was dropped" \
    "no" "$(exists "$CREW_B/memory.db-wal")"

# --- disabling the feature leaves paths verbatim ----------------------------
rm -rf "$BUNDLE"
(
    export HOME="$HOME_A" KIROCREW_DIR="$CREW_A" SYNC_BACKEND=rsync SYNC_PORTABLE_PATHS=0
    # shellcheck source=/dev/null
    source "$TOOL/kirocrew-sync.sh" help > /dev/null
    set +e
    prepare_sync_bundle "$BUNDLE" > /dev/null
    translate_bundle_paths "$BUNDLE" encode > /dev/null
) || { echo "  opt-out step failed" >&2; exit 1; }

assert_eq "SYNC_PORTABLE_PATHS=0 sends the path verbatim" \
    "$HOME_A/code/groover/docs" "$(db_uri "$BUNDLE/workspace/knowledge/knowledge.db")"

# --- the running-KiroCrew guard does not trip over this script --------------
#
# The guard greps the process table for "kirocrew", which the sync script's own
# command line contains. Running a copy under a unique name -- and having it
# look for that name instead -- reproduces the self-match on any machine,
# whether or not the real KiroCrew is running here.
# The copy lives beside the original so it still finds config.sh and backends/.
FAKE="$TOOL/zzsync-marker.sh"
sed 's/pgrep -f "kirocrew"/pgrep -f "zzsync-marker"/' "$TOOL/kirocrew-sync.sh" > "$FAKE"
printf '\ncheck_kirocrew_running && echo GUARD_CLEAR || echo GUARD_BLOCKED\n' >> "$FAKE"

GUARD_OUT="$(HOME="$HOME_A" KIROCREW_DIR="$CREW_A" SYNC_BACKEND=rsync \
    bash "$FAKE" help 2>&1 | tail -1)"

assert_eq "the guard does not mistake the sync script for KiroCrew" \
    "GUARD_CLEAR" "$GUARD_OUT"

echo
if [ "$FAIL" -eq 0 ]; then
    printf '\033[0;32m✓\033[0m %d passed\n' "$PASS"
    exit 0
fi
printf '\033[0;31m✗\033[0m %d passed, %d failed\n' "$PASS" "$FAIL"
exit 1
