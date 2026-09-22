#!/usr/bin/env bash
#
# Per-member memory stores (<data home>/memory_stores/<name>/, KiroCrew 0.7).
#
# Each store's memory.db is discovered at sync time and synced row by row as
# its own logical database, memory_stores/<name>. Its markdown travels as
# files. Host-local neighbours (the member API key, the member backup
# directory, execution logs, a store's own backups/) never leave the machine,
# and team scope never publishes a store at all.
#
# Usage: tests/test_member_stores.sh [workdir]
#

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$TEST_DIR/.." && pwd)"
WORK="${1:-$(mktemp -d)}"

FIXTURE="python3 $TEST_DIR/fixture.py"
PASS=0
FAIL=0

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

ok()    { echo -e "  ${GREEN}PASS${NC} $*"; PASS=$((PASS + 1)); }
bad()   { echo -e "  ${RED}FAIL${NC} $*"; FAIL=$((FAIL + 1)); }
head_() { echo; echo -e "${YELLOW}== $*${NC}"; }

# Here-strings and grep -a, for the reasons spelled out in run_tests.sh: a
# pipe under pipefail can race on SIGPIPE, and published dumps hold binary.
assert_contains() {
    if grep -aqF -- "$3" <<< "$2"; then ok "$1"; else
        bad "$1"; echo "      expected to find: $3"
    fi
}
assert_not_contains() {
    if grep -aqF -- "$3" <<< "$2"; then
        bad "$1"; echo "      should not contain: $3"
    else ok "$1"; fi
}
assert_eq() {
    if [ "$2" = "$3" ]; then ok "$1"; else
        bad "$1"; echo "      left:  $2"; echo "      right: $3"
    fi
}

# --------------------------------------------------------------------------

STORE="member-alice-0123456789abcdef"

setup_machine() {
    local name="$1" dir="$WORK/$1"
    rm -rf "$dir"
    mkdir -p "$dir"
    $FIXTURE create "$dir" --name "$name" > /dev/null
    echo "machine-$name" > "$dir/.machine_id"
    cat > "$WORK/config-$name.sh" << EOF
export SYNC_BACKEND="local"
export KIROCREW_DIR="$dir"
export LOCAL_SYNC_DIR="$WORK/remote"
EOF
}

sync_machine() {
    local name="$1"; shift
    KIROCREW_SYNC_CONFIG="$WORK/config-$name.sh" \
    KIROCREW_SYNC_SKIP_RUNNING_CHECK=1 \
        "$ROOT/kirocrew-sync.sh" "$@" 2>&1
}

stores() { $FIXTURE store-dump "$WORK/$1"; }

# jq-free field extraction from store-dump output.
field() {
    python3 -c 'import json,sys
d=json.load(sys.stdin)
v=d.get(sys.argv[1], {})
for k in sys.argv[2:]:
    v=v[k] if isinstance(v, dict) else [r[k] for r in v]
print(json.dumps(v, sort_keys=True))' "$@"
}

# Everything that reached the backend, as plain text (see run_tests.sh).
published_dump() {
    local tmp bundle
    tmp="$(mktemp -d)"
    git init -q "$tmp"
    for bundle in "$WORK"/remote/bundles/*.bundle; do
        [ -f "$bundle" ] || continue
        git -C "$tmp" fetch -q "$bundle" \
            "refs/heads/*:refs/remotes/$(basename "$bundle" .bundle)/*" 2>/dev/null || true
    done
    git -C "$tmp" rev-list --all --objects 2>/dev/null | awk 'NF > 1 {print $2}' | sort -u
    git -C "$tmp" cat-file --batch-all-objects --batch 2>/dev/null | tr -d '\000'
    rm -rf "$tmp"
}

fresh_pair() {
    rm -rf "$WORK/remote"
    setup_machine a
    setup_machine b
    sync_machine a sync > /dev/null 2>&1
    sync_machine b sync > /dev/null 2>&1
    sync_machine a sync > /dev/null 2>&1
}

round() {
    sync_machine a sync > /dev/null 2>&1
    sync_machine b sync > /dev/null 2>&1
    sync_machine a sync > /dev/null 2>&1
}

# --------------------------------------------------------------------------

echo "workdir: $WORK"

head_ "Scenario 1: a store created on one machine appears on the other"
fresh_pair
$FIXTURE create-store "$WORK/a" "$STORE" --member-id alice > /dev/null
$FIXTURE store-episode "$WORK/a" "$STORE" ep-a1 "episode text from A" \
    --ts 2026-03-01T09:00:00+00:00
$FIXTURE store-history "$WORK/a" "$STORE" 2026-03-01 "# 2026-03-01 history on A" \
    --ts 2026-03-01T09:00:00+00:00
OUT="$(sync_machine a sync)"
OUT_B="$(sync_machine b sync)"
assert_contains "B reports creating the store" "$OUT_B" "created memory store $STORE"

SA="$(stores a)"; SB="$(stores b)"
assert_eq "B has the store's database" \
    "$([ -f "$WORK/b/memory_stores/$STORE/memory.db" ] && echo yes || echo no)" "yes"
assert_eq "identity row travelled (member id, store id)" \
    "$(field "$STORE" identity <<< "$SB")" '[["alice", "'"$STORE"'"]]'
assert_contains "the lesson arrived"          "$SB" "seed lesson of $STORE"
assert_contains "the episode arrived"         "$SB" "episode text from A"
assert_contains "the history day arrived"     "$SB" "history on A"
assert_contains "the embedding arrived"       "$(field "$STORE" items emb <<< "$SB")" "1024"
assert_contains "preferences.md arrived"      "$SB" "prefers tabs"
assert_eq "views were recreated" \
    "$(field "$STORE" views <<< "$SB")" '["episodic_memories", "semantic_memory"]'
assert_eq "lessons readable through the view" \
    "$(field "$STORE" lessons_view <<< "$SB")" '["lesson.seed-'"$STORE"'"]'
# memory_fts is derived: never synced, rebuilt from memory_items + history.
assert_eq "FTS rebuilt with the arrived rows" \
    "$(field "$STORE" fts <<< "$SB")" \
    '["ep-a1", "history:2026-03-01", "key:lesson.seed-'"$STORE"'"]'
assert_eq "store directory is owner-only" \
    "$(stat -c %a "$WORK/b/memory_stores/$STORE")" "700"
assert_eq "store database is owner-only" \
    "$(stat -c %a "$WORK/b/memory_stores/$STORE/memory.db")" "600"
assert_eq "store markdown is owner-only" \
    "$(stat -c %a "$WORK/b/memory_stores/$STORE/memory/preferences.md")" "600"
sync_machine a sync > /dev/null
assert_eq "machines converged" "$(stores a)" "$(stores b)"

head_ "Scenario 2: host-local store state never leaves the machine"
assert_eq "no member API key on B" \
    "$([ -e "$WORK/b/memory_stores/.member-api-key" ] && echo present || echo absent)" "absent"
assert_eq "no member backups on B" \
    "$([ -e "$WORK/b/memory_stores/.member-backups" ] && echo present || echo absent)" "absent"
assert_eq "no execution logs on B" \
    "$([ -e "$WORK/b/memory_stores/.execution-logs" ] && echo present || echo absent)" "absent"
assert_eq "no store backups/ on B" \
    "$([ -e "$WORK/b/memory_stores/$STORE/backups" ] && echo present || echo absent)" "absent"
SCAN="$(published_dump)"
assert_contains     "scan is non-vacuous"             "$SCAN" "seed lesson of $STORE"
assert_contains     "store published as rows"         "$SCAN" "db/memory_stores/$STORE/memory_items.jsonl"
assert_not_contains "memory.db never published as a file" "$SCAN" "memory_stores/$STORE/memory.db"
assert_not_contains "no member API key published"     "$SCAN" "MEMBER-API-KEY"
assert_not_contains "no member backup published"      "$SCAN" "MEMBER-BACKUP-CONTENT"
assert_not_contains "no store backup published"       "$SCAN" "STORE-BACKUP-CONTENT"
assert_not_contains "no execution log published"      "$SCAN" "EXECUTION-LOG"
assert_not_contains "memory_fts rows not published"   "$SCAN" "memory_fts.jsonl"
assert_eq "A still has its own host-local key" \
    "$(cat "$WORK/a/memory_stores/.member-api-key")" "MEMBER-API-KEY-$STORE"

head_ "Scenario 2b: a named V1 store's files travel, its derived index does not"
V1="coding"
mkdir -p "$WORK/a/memory_stores/$V1/memory/history"
echo "# coding prefs" > "$WORK/a/memory_stores/$V1/memory/preferences.md"
echo "# 2026-03-01 coding history" > "$WORK/a/memory_stores/$V1/memory/history/2026-03-01.md"
echo '{"ts":"2026-03-01","rule":"V1-STORE-LESSON","category":"tool"}' \
    > "$WORK/a/memory_stores/$V1/lessons.jsonl"
echo "V1-INDEX-BYTES" > "$WORK/a/memory_stores/$V1/memory_index.db"
round
assert_contains "V1 lessons.jsonl arrived" \
    "$(cat "$WORK/b/memory_stores/$V1/lessons.jsonl" 2>&1)" "V1-STORE-LESSON"
assert_contains "V1 history arrived" \
    "$(cat "$WORK/b/memory_stores/$V1/memory/history/2026-03-01.md" 2>&1)" "coding history"
assert_eq "V1 derived memory_index.db stayed home" \
    "$([ -e "$WORK/b/memory_stores/$V1/memory_index.db" ] && echo present || echo absent)" "absent"
assert_not_contains "V1 index never published" "$(published_dump)" "V1-INDEX-BYTES"

head_ "Scenario 3: the same store on both machines merges row by row"
$FIXTURE store-write   "$WORK/a" "$STORE" lesson.from-a "lesson written on A" \
    --ts 2026-03-02T10:00:00+00:00
$FIXTURE store-write   "$WORK/b" "$STORE" lesson.from-b "lesson written on B" \
    --ts 2026-03-02T11:00:00+00:00
$FIXTURE store-episode "$WORK/b" "$STORE" ep-b1 "episode text from B" \
    --ts 2026-03-02T11:30:00+00:00
round
SA="$(stores a)"; SB="$(stores b)"
assert_contains "A kept its lesson"        "$SA" "lesson written on A"
assert_contains "A received B's lesson"    "$SA" "lesson written on B"
assert_contains "A received B's episode"   "$SA" "episode text from B"
assert_contains "B received A's lesson"    "$SB" "lesson written on A"
assert_eq       "machines converged"       "$SA" "$SB"
# AUTOINCREMENT ids are machine-relative: both logs are unioned on their
# natural key and renumbered identically on both sides.
assert_eq "event ids contiguous and equal" \
    "$(field "$STORE" events id <<< "$SA")" "[1, 2, 3]"
assert_eq "revision ids contiguous and equal" \
    "$(field "$STORE" revisions id <<< "$SB")" "[1, 2, 3]"
assert_contains "B's FTS indexes A's lesson" \
    "$(field "$STORE" fts <<< "$SB")" "key:lesson.from-a"

head_ "Scenario 4: the same row edited on both machines - newer wins"
$FIXTURE store-write "$WORK/a" "$STORE" lesson.from-a "edited on A (older)" \
    --ts 2026-03-03T09:00:00+00:00
$FIXTURE store-write "$WORK/b" "$STORE" lesson.from-a "edited on B (newer)" \
    --ts 2026-03-03T18:00:00+00:00
round
SA="$(stores a)"; SB="$(stores b)"
assert_contains     "newer edit won on A"  "$SA" "edited on B (newer)"
assert_not_contains "older edit dropped"   "$SA" "edited on A (older)"
assert_eq           "machines converged"   "$SA" "$SB"

head_ "Scenario 5: idle syncs are a no-op"
BEFORE="$(stores a)"
round
sync_machine b sync > /dev/null; RC=$?
assert_eq "idle syncs change nothing" "$BEFORE" "$(stores a)"
assert_eq "idle sync exits 0"         "$RC" "0"
assert_eq "working tree clean" \
    "$(git -C "$WORK/a/.sync/repo" status --porcelain | wc -l | tr -d ' ')" "0"

head_ "Scenario 6: a store whose schema differs quarantines that machine"
$FIXTURE store-sql "$WORK/a" "$STORE" \
    "ALTER TABLE memory_items ADD COLUMN future_column TEXT"
$FIXTURE store-write "$WORK/a" "$STORE" lesson.after-upgrade "written after A upgraded" \
    --ts 2026-03-04T10:00:00+00:00
sync_machine a sync > /dev/null 2>&1
OUT="$(sync_machine b sync)"; RC=$?
assert_contains "store drift detected"     "$OUT" "memory_stores/$STORE: schema drift"
assert_contains "machine was quarantined"  "$OUT" "quarantined"
assert_eq       "exit code says partial"   "$RC" "3"
assert_not_contains "quarantined rows stayed out" "$(stores b)" "written after A upgraded"
# Undo the drift on A (a real fix is B upgrading too); the quarantine lifts.
$FIXTURE store-sql "$WORK/a" "$STORE" "ALTER TABLE memory_items DROP COLUMN future_column"
sync_machine a sync > /dev/null 2>&1
OUT="$(sync_machine b sync)"; RC=$?
assert_eq       "quarantine lifts once schemas agree" "$RC" "0"
assert_contains "rows arrive after the lift" "$(stores b)" "written after A upgraded"

head_ "Scenario 7: the same store embedded with two models quarantines"
$FIXTURE store-sql "$WORK/a" "$STORE" \
    "UPDATE memory_meta SET value='sig-other-model', updated_at='2026-03-05T00:00:00+00:00' WHERE key='embedding_space_sig'"
$FIXTURE store-write "$WORK/a" "$STORE" lesson.other-model "embedded with another model" \
    --ts 2026-03-05T10:00:00+00:00
sync_machine a sync > /dev/null 2>&1
OUT="$(sync_machine b sync)"; RC=$?
assert_contains "store embedding mismatch detected" "$OUT" \
    "memory_stores/$STORE: embedding space mismatch"
assert_eq       "exit code says partial" "$RC" "3"
assert_not_contains "mismatched vectors stayed out" "$(stores b)" "embedded with another model"

head_ "Scenario 8: store names are validated; links are not followed"
fresh_pair
$FIXTURE create-store "$WORK/a" "$STORE" --member-id alice > /dev/null
# Not a store name KiroCrew can produce: uppercase and a dot.
$FIXTURE create-store "$WORK/a" "Bad.Name" --member-id mallory > /dev/null
echo "INVALID-STORE-PREFS" > "$WORK/a/memory_stores/Bad.Name/memory/preferences.md"
# An alias of another store. Upstream refuses it (identity, not containment).
ln -s "$WORK/a/memory_stores/$STORE" "$WORK/a/memory_stores/member-alias"
OUT="$(sync_machine a sync)"
sync_machine b sync > /dev/null
SCAN="$(published_dump)"
assert_contains     "the real store was published" "$SCAN" "db/memory_stores/$STORE/"
assert_not_contains "invalid name not published"   "$SCAN" "Bad.Name"
assert_not_contains "invalid store's files not published" "$SCAN" "INVALID-STORE-PREFS"
assert_not_contains "aliased store not published"  "$SCAN" "member-alias"
assert_contains     "the alias was reported"       "$OUT" "member-alias"
assert_eq "B has only the real store" \
    "$(ls "$WORK/b/memory_stores" | tr '\n' ' ')" "$STORE "
# The receiving side too: a store arriving from A must not be packed through
# a link on B that points at another store.
STORE2="member-bob-fedcba9876543210"
$FIXTURE create-store "$WORK/a" "$STORE2" --member-id bob > /dev/null
ln -s "$WORK/b/memory_stores/$STORE" "$WORK/b/memory_stores/$STORE2"
sync_machine a sync > /dev/null
OUT="$(sync_machine b sync)"
assert_contains "B refused to pack through the link" "$OUT" \
    "not packing memory store $STORE2"
assert_not_contains "B's other store was not touched" \
    "$(field "$STORE" items key <<< "$(stores b)")" "lesson.seed-$STORE2"

head_ "Scenario 9: team scope never publishes a member store"
rm -rf "$WORK/remote"
setup_machine alice
setup_machine bob
$FIXTURE create-store "$WORK/alice" "$STORE" --member-id alice > /dev/null
$FIXTURE store-write "$WORK/alice" "$STORE" lesson.alice-private "alice's private member lesson" \
    --ts 2026-03-01T10:00:00+00:00
$FIXTURE store-episode "$WORK/alice" "$STORE" ep-private "alice's private member episode" \
    --ts 2026-03-01T10:00:00+00:00
OUT="$(sync_machine alice sync --team)"
sync_machine bob sync --team > /dev/null
sync_machine alice sync --team > /dev/null
assert_contains "held back, and said so" "$OUT" "member memory store(s) private"
SCAN="$(published_dump)"
assert_contains     "scan is non-vacuous"            "$SCAN" "item-common"
assert_not_contains "no store rows published"        "$SCAN" "memory_items.jsonl"
assert_not_contains "no store name published"        "$SCAN" "$STORE"
assert_not_contains "no member lesson published"     "$SCAN" "alice's private member lesson"
assert_not_contains "no member episode published"    "$SCAN" "alice's private member episode"
assert_not_contains "no member preferences published" "$SCAN" "prefers tabs"
assert_eq "bob has no stores" \
    "$([ -e "$WORK/bob/memory_stores" ] && echo present || echo absent)" "absent"
assert_contains "alice keeps her store" "$(stores alice)" "alice's private member lesson"
assert_eq "team repo has no store tree" \
    "$([ -e "$WORK/alice/.sync/repo-team/db/memory_stores" ] && echo present || echo absent)" "absent"

# --------------------------------------------------------------------------

echo
echo "-----------------------------------------"
echo -e "  ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}"
echo "  workdir: $WORK"
echo "-----------------------------------------"
[ "$FAIL" -eq 0 ]
