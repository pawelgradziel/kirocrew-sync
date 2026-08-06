#!/usr/bin/env bash
#
# Tests for lib/portable_paths.py
#
# Machines are simulated by overriding $HOME: Path.home() reads it on every
# call, so "encode with HOME=/home/alice, decode with HOME=/Users/pawel" is a
# faithful stand-in for syncing between a Linux laptop and a Mac.
#
# Run: ./tests/test_portable_paths.sh
#

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$SCRIPT_DIR/../lib/portable_paths.py"

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

assert_contains() {
    local name="$1" needle="$2" haystack="$3"
    case "$haystack" in
        *"$needle"*)
            printf '  \033[0;32m✓\033[0m %s\n' "$name"
            PASS=$((PASS + 1)) ;;
        *)
            printf '  \033[0;31m✗\033[0m %s\n      missing: %s\n      in:       %s\n' \
                "$name" "$needle" "$haystack"
            FAIL=$((FAIL + 1)) ;;
    esac
}

# --- database helpers ------------------------------------------------------

db_new() {
    python3 - "$1" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.executescript("""
CREATE TABLE sources (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, source_type TEXT NOT NULL,
    uri TEXT UNIQUE NOT NULL, properties TEXT DEFAULT '{}', last_synced TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE folder_file_state (
    source_id TEXT NOT NULL, file_path TEXT NOT NULL, content_hash TEXT,
    mtime REAL, item_ids TEXT DEFAULT '[]', last_seen TEXT NOT NULL,
    status TEXT DEFAULT 'pending', error_message TEXT,
    PRIMARY KEY (source_id, file_path));
CREATE TABLE dismissed_auto_sources (uri TEXT PRIMARY KEY, dismissed_at TEXT NOT NULL);
CREATE TABLE items (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
    item_type TEXT NOT NULL, source_id TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
""")
db.commit()
db.close()
PY
}

# db_add_source <db> <name> <uri> [source_type]
db_add_source() {
    python3 - "$@" <<'PY'
import sqlite3, sys
path, name, uri = sys.argv[1:4]
stype = sys.argv[4] if len(sys.argv) > 4 else "local_folder"
db = sqlite3.connect(path)
db.execute("INSERT INTO sources (id, name, source_type, uri, created_at, updated_at)"
           " VALUES (?, ?, ?, ?, 'now', 'now')", (name, name, stype, uri))
db.commit()
db.close()
PY
}

# db_add_file <db> <source_id> <file_path>
db_add_file() {
    python3 - "$@" <<'PY'
import sqlite3, sys
path, source_id, file_path = sys.argv[1:4]
db = sqlite3.connect(path)
db.execute("INSERT INTO folder_file_state (source_id, file_path, last_seen)"
           " VALUES (?, ?, 'now')", (source_id, file_path))
db.commit()
db.close()
PY
}

# db_add_item <db> <id> <source_id>
db_add_item() {
    python3 - "$@" <<'PY'
import sqlite3, sys
path, item_id, source_id = sys.argv[1:4]
db = sqlite3.connect(path)
db.execute("INSERT INTO items (id, title, content, item_type, source_id, created_at, updated_at)"
           " VALUES (?, 'doc', 'body', 'document', ?, 'now', 'now')", (item_id, source_id))
db.commit()
db.close()
PY
}

# db_query <db> <sql> -- one scalar or newline-joined column
db_query() {
    python3 - "$@" <<'PY'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print("\n".join(str(r[0]) for r in db.execute(sys.argv[2])))
db.close()
PY
}

db_uri() { db_query "$1" "SELECT uri FROM sources WHERE id = '$2'"; }

new_case() {
    CASE_DIR="$WORK/case-$((++CASE_N))"
    mkdir -p "$CASE_DIR"
    DB="$CASE_DIR/knowledge.db"
    db_new "$DB"
}
CASE_N=0

# encode/decode with a simulated home directory and optional map file
run_tool() {
    local home="$1" cmd="$2" db="$3" map="${4:-}"
    HOME="$home" KIROCREW_PATH_MAP="$map" python3 "$TOOL" "$cmd" "$db" 2>&1
}

echo "portable_paths.py"
echo

# --- 1. absolute path under $HOME becomes a tilde path ---------------------
new_case
db_add_source "$DB" laptop-docs /home/alice/dev/groover/docs
run_tool /home/alice encode "$DB" > /dev/null
assert_eq "absolute path under \$HOME encodes to ~" \
    "~/dev/groover/docs" "$(db_uri "$DB" laptop-docs)"

# --- 2. absolute path outside $HOME stays absolute -------------------------
new_case
db_add_source "$DB" shared /opt/shared/docs
run_tool /home/alice encode "$DB" > /dev/null
assert_eq "absolute path outside \$HOME stays absolute" \
    "/opt/shared/docs" "$(db_uri "$DB" shared)"

# --- 3. encoding is idempotent --------------------------------------------
new_case
db_add_source "$DB" already-portable "~/dev/groover/docs"
run_tool /home/alice encode "$DB" > /dev/null
run_tool /home/alice encode "$DB" > /dev/null
assert_eq "tilde path is left alone (idempotent)" \
    "~/dev/groover/docs" "$(db_uri "$DB" already-portable)"

# --- 4. relative paths are reported, never guessed at ----------------------
new_case
db_add_source "$DB" relative "./documentation"
OUT="$(run_tool /home/alice encode "$DB")"
assert_eq "relative path is left untouched" \
    "./documentation" "$(db_uri "$DB" relative)"
assert_contains "relative path is reported" "relative path left as-is" "$OUT"

# --- 5. non-path URIs are never rewritten ---------------------------------
new_case
db_add_source "$DB" artifacts "artifact://" artifact
db_add_source "$DB" web "https://example.com/docs" web
run_tool /home/alice encode "$DB" > /dev/null
assert_eq "artifact:// URI untouched" "artifact://" "$(db_uri "$DB" artifacts)"
assert_eq "https:// URI untouched" "https://example.com/docs" "$(db_uri "$DB" web)"

# --- 6. decode lands on the receiving machine's home ----------------------
new_case
db_add_source "$DB" docs /home/alice/dev/groover/docs
run_tool /home/alice encode "$DB" > /dev/null
run_tool /home/bob decode "$DB" > /dev/null
assert_eq "tilde path decodes to the receiving machine's \$HOME" \
    "/home/bob/dev/groover/docs" "$(db_uri "$DB" docs)"

# --- 7. Linux -> macOS -> Linux roundtrip ---------------------------------
new_case
db_add_source "$DB" docs /home/pawel/code/groover
run_tool /home/pawel encode "$DB" > /dev/null
run_tool /Users/pawel decode "$DB" > /dev/null
assert_eq "Linux path lands correctly on macOS" \
    "/Users/pawel/code/groover" "$(db_uri "$DB" docs)"
run_tool /Users/pawel encode "$DB" > /dev/null
assert_eq "macOS path re-encodes to the same portable form" \
    "~/code/groover" "$(db_uri "$DB" docs)"
run_tool /home/pawel decode "$DB" > /dev/null
assert_eq "roundtrip returns the original Linux path" \
    "/home/pawel/code/groover" "$(db_uri "$DB" docs)"

# --- 8. path map carries different $HOME layouts --------------------------
new_case
printf 'CODE = /home/alice/dev\n' > "$CASE_DIR/map-a.conf"
printf 'CODE = /home/bob/work/projects\n' > "$CASE_DIR/map-b.conf"
db_add_source "$DB" docs /home/alice/dev/groover/docs
run_tool /home/alice encode "$DB" "$CASE_DIR/map-a.conf" > /dev/null
assert_eq "mapped prefix encodes to a variable" \
    '${CODE}/groover/docs' "$(db_uri "$DB" docs)"
run_tool /home/bob decode "$DB" "$CASE_DIR/map-b.conf" > /dev/null
assert_eq "variable decodes to this machine's layout" \
    "/home/bob/work/projects/groover/docs" "$(db_uri "$DB" docs)"

# --- 9. a location outside $HOME travels when mapped ----------------------
new_case
printf 'SHARED = /mnt/shared\n' > "$CASE_DIR/map-a.conf"
printf 'SHARED = /Volumes/shared\n' > "$CASE_DIR/map-b.conf"
db_add_source "$DB" wiki /mnt/shared/wiki
run_tool /home/alice encode "$DB" "$CASE_DIR/map-a.conf" > /dev/null
assert_eq "path outside \$HOME encodes via its mapping" \
    '${SHARED}/wiki' "$(db_uri "$DB" wiki)"
run_tool /Users/pawel decode "$DB" "$CASE_DIR/map-b.conf" > /dev/null
assert_eq "path outside \$HOME decodes on another OS" \
    "/Volumes/shared/wiki" "$(db_uri "$DB" wiki)"

# --- 10. longest matching prefix wins -------------------------------------
new_case
printf 'CODE = /home/alice/code\nWORK = /home/alice/code/work\n' > "$CASE_DIR/map.conf"
db_add_source "$DB" nested /home/alice/code/work/groover
run_tool /home/alice encode "$DB" "$CASE_DIR/map.conf" > /dev/null
assert_eq "most specific mapping wins" \
    '${WORK}/groover' "$(db_uri "$DB" nested)"

# --- 11. an unmapped variable is left visible, never guessed --------------
new_case
db_add_source "$DB" wiki '${SHARED}/wiki'
OUT="$(run_tool /home/bob decode "$DB")"
assert_eq "unmapped variable leaves the path untouched" \
    '${SHARED}/wiki' "$(db_uri "$DB" wiki)"
assert_contains "unmapped variable is reported" "no mapping for \${SHARED}" "$OUT"
assert_contains "unmapped variable suggests the fix" "SHARED = /your/local/path" "$OUT"

# --- 12. per-file ingest state travels with its source --------------------
new_case
db_add_source "$DB" docs /home/alice/dev/groover/docs
db_add_file "$DB" docs /home/alice/dev/groover/docs/readme.md
db_add_file "$DB" docs /home/alice/dev/groover/docs/guide.md
run_tool /home/alice encode "$DB" > /dev/null
assert_eq "folder file state encodes too" \
    "~/dev/groover/docs/guide.md
~/dev/groover/docs/readme.md" \
    "$(db_query "$DB" "SELECT file_path FROM folder_file_state ORDER BY file_path")"
run_tool /home/bob decode "$DB" > /dev/null
assert_eq "folder file state decodes to the local layout" \
    "/home/bob/dev/groover/docs/guide.md
/home/bob/dev/groover/docs/readme.md" \
    "$(db_query "$DB" "SELECT file_path FROM folder_file_state ORDER BY file_path")"

# --- 13. auto-source tombstones travel too --------------------------------
new_case
python3 - "$DB" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute("INSERT INTO dismissed_auto_sources (uri, dismissed_at) VALUES ('/home/alice/dev/skip', 'now')")
db.commit()
db.close()
PY
run_tool /home/alice encode "$DB" > /dev/null
assert_eq "dismissed auto-source encodes" \
    "~/dev/skip" "$(db_query "$DB" "SELECT uri FROM dismissed_auto_sources")"

# --- 14. symlinks keep their logical path ---------------------------------
new_case
mkdir -p "$CASE_DIR/home/real/docs"
ln -s "$CASE_DIR/home/real" "$CASE_DIR/home/link"
db_add_source "$DB" linked "$CASE_DIR/home/link/docs"
run_tool "$CASE_DIR/home" encode "$DB" > /dev/null
assert_eq "symlink is not resolved away" \
    "~/link/docs" "$(db_uri "$DB" linked)"

# --- 15. a collision after normalization loses nothing --------------------
new_case
db_add_source "$DB" absolute /home/alice/dev/docs
db_add_source "$DB" tilde "~/dev/docs"
db_add_item "$DB" item-1 absolute
db_add_item "$DB" item-2 tilde
OUT="$(run_tool /home/alice encode "$DB")"
assert_eq "both sources survive a collision" \
    "2" "$(db_query "$DB" "SELECT count(*) FROM sources")"
assert_eq "no items are lost to a collision" \
    "2" "$(db_query "$DB" "SELECT count(*) FROM items")"
assert_contains "the collision is reported" "duplicate after normalization" "$OUT"

# --- 16. nothing is lost across a full push/pull cycle --------------------
new_case
db_add_source "$DB" docs /home/alice/dev/groover/docs
db_add_source "$DB" notes /home/alice/notes
db_add_source "$DB" artifacts "artifact://" artifact
db_add_source "$DB" outside /opt/shared/wiki
for i in 1 2 3 4 5; do db_add_item "$DB" "item-$i" docs; done
BEFORE="$(db_query "$DB" "SELECT count(*) FROM items")"
run_tool /home/alice encode "$DB" > /dev/null
run_tool /home/alice decode "$DB" > /dev/null
assert_eq "item count is preserved" "$BEFORE" "$(db_query "$DB" "SELECT count(*) FROM items")"
assert_eq "source paths return unchanged" \
    "/home/alice/dev/groover/docs
/home/alice/notes
/opt/shared/wiki
artifact://" \
    "$(db_query "$DB" "SELECT uri FROM sources ORDER BY uri")"

# --- 17. a write-ahead log is folded into the database --------------------
new_case
db_add_source "$DB" docs /home/alice/dev/groover/docs
python3 - "$DB" <<'PY'
import os, sqlite3, sys
# Leave a live -wal behind, the way a copied bundle carries one: os._exit skips
# the clean close that would otherwise checkpoint and remove it.
db = sqlite3.connect(sys.argv[1])
db.execute("PRAGMA journal_mode=WAL")
db.execute("INSERT INTO sources (id, name, source_type, uri, created_at, updated_at)"
           " VALUES ('wal', 'wal', 'local_folder', '/home/alice/dev/in-wal', 'now', 'now')")
db.commit()
os._exit(0)
PY
assert_eq "test setup leaves a -wal file" "yes" "$([ -f "$DB-wal" ] && echo yes || echo no)"
run_tool /home/alice encode "$DB" > /dev/null
assert_eq "-wal is folded into the database" "no" "$([ -f "$DB-wal" ] && echo yes || echo no)"
assert_eq "data held in the -wal is encoded, not lost" \
    "~/dev/in-wal" "$(db_uri "$DB" wal)"

# --- 18. a broken map file fails loudly, without touching the database ----
new_case
printf 'CODE ~/dev\n' > "$CASE_DIR/bad.conf"
db_add_source "$DB" docs /home/alice/dev/groover/docs
OUT="$(run_tool /home/alice encode "$DB" "$CASE_DIR/bad.conf")"
STATUS=$?
assert_eq "a malformed map file is an error" "2" "$STATUS"
assert_contains "the bad line is named" "expected 'NAME = /local/path'" "$OUT"
assert_eq "the database is untouched after a map error" \
    "/home/alice/dev/groover/docs" "$(db_uri "$DB" docs)"

# --- 19. a relative mapping target is rejected ----------------------------
new_case
printf 'CODE = dev\n' > "$CASE_DIR/bad.conf"
db_add_source "$DB" docs /home/alice/dev/groover/docs
OUT="$(run_tool /home/alice encode "$DB" "$CASE_DIR/bad.conf")"
assert_contains "a relative mapping target is rejected" "must map to an absolute path" "$OUT"

# --- 20. report tells the user where each source stands -------------------
new_case
mkdir -p "$CASE_DIR/home/dev/groover/docs"
db_add_source "$DB" present "$CASE_DIR/home/dev/groover/docs"
db_add_source "$DB" missing "$CASE_DIR/home/dev/gone"
db_add_source "$DB" outside /opt/shared/wiki
OUT="$(run_tool "$CASE_DIR/home" report "$DB")"
assert_contains "report shows the portable form" "syncs as ~/dev/groover/docs" "$OUT"
assert_contains "report flags a missing path" "path not found on this machine" "$OUT"
assert_contains "report flags an unportable path" "outside \$HOME and unmapped" "$OUT"

# --- 21. a missing database is not an error -------------------------------
OUT="$(run_tool /home/alice encode "$WORK/nope/knowledge.db")"
STATUS=$?
assert_eq "a missing database exits cleanly" "0" "$STATUS"
assert_contains "a missing database says so" "nothing to do" "$OUT"

echo
if [ "$FAIL" -eq 0 ]; then
    printf '\033[0;32m✓\033[0m %d passed\n' "$PASS"
    exit 0
fi
printf '\033[0;31m✗\033[0m %d passed, %d failed\n' "$PASS" "$FAIL"
exit 1
