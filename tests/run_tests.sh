#!/usr/bin/env bash
#
# Two-machine sync tests. Everything runs against throwaway directories and a
# local-directory backend; no network and no real KiroCrew data is touched.
#
# Usage: tests/run_tests.sh [workdir]
#

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$TEST_DIR/.." && pwd)"
WORK="${1:-$(mktemp -d)}"

FIXTURE="python3 $TEST_DIR/fixture.py"
PASS=0
FAIL=0

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

ok()   { echo -e "  ${GREEN}PASS${NC} $*"; PASS=$((PASS + 1)); }
bad()  { echo -e "  ${RED}FAIL${NC} $*"; FAIL=$((FAIL + 1)); }
head_() { echo; echo -e "${YELLOW}== $*${NC}"; }

assert_contains() {
    if echo "$2" | grep -qF -- "$3"; then ok "$1"; else
        bad "$1"
        echo "      expected to find: $3"
    fi
}

assert_not_contains() {
    if echo "$2" | grep -qF -- "$3"; then
        bad "$1"
        echo "      should not contain: $3"
    else ok "$1"; fi
}

assert_eq() {
    if [ "$2" = "$3" ]; then ok "$1"; else
        bad "$1"
        echo "      left:  $2"
        echo "      right: $3"
    fi
}

# --------------------------------------------------------------------------

setup_machine() {
    local name="$1" sig="${2:-sig-shared}"
    local dir="$WORK/$name"
    rm -rf "$dir"
    mkdir -p "$dir"
    $FIXTURE create "$dir" --name "$name" --embedding-sig "$sig" > /dev/null
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

dump()       { $FIXTURE dump "$WORK/$1"; }
local_only() { $FIXTURE local-only "$WORK/$1"; }

fresh_pair() {
    rm -rf "$WORK/remote"
    setup_machine a
    setup_machine b
    # Establish a shared base: a publishes, b merges it in.
    sync_machine a sync > /dev/null 2>&1
    sync_machine b sync > /dev/null 2>&1
    sync_machine a sync > /dev/null 2>&1
}

# --------------------------------------------------------------------------

echo "workdir: $WORK"

head_ "Scenario 1: disjoint changes on both machines are both kept"
fresh_pair
$FIXTURE set-lesson "$WORK/a" lesson.from-a "written on A" --ts 2026-03-01T10:00:00+00:00
$FIXTURE add-item   "$WORK/a" item-a "Doc A" --ts 2026-03-01T10:00:00+00:00
$FIXTURE set-lesson "$WORK/b" lesson.from-b "written on B" --ts 2026-03-01T11:00:00+00:00
$FIXTURE add-item   "$WORK/b" item-b "Doc B" --ts 2026-03-01T11:00:00+00:00

sync_machine a sync > /dev/null
sync_machine b sync > /dev/null
sync_machine a sync > /dev/null

DA="$(dump a)"; DB="$(dump b)"
assert_contains "A has its own lesson"     "$DA" "lesson.from-a"
assert_contains "A received B's lesson"    "$DA" "lesson.from-b"
assert_contains "B has its own lesson"     "$DB" "lesson.from-b"
assert_contains "B received A's lesson"    "$DB" "lesson.from-a"
assert_contains "A received B's item"      "$DA" "item-b"
assert_contains "B received A's item"      "$DB" "item-a"
assert_eq       "machines converged"       "$DA" "$DB"

head_ "Scenario 2: same row edited on both sides - newer wins, both converge"
fresh_pair
$FIXTURE set-lesson "$WORK/a" lesson.conflict "edited on A" --ts 2026-03-01T09:00:00+00:00
$FIXTURE set-lesson "$WORK/b" lesson.conflict "edited on B" --ts 2026-03-01T18:00:00+00:00

OUT_A="$(sync_machine a sync)"
OUT_B="$(sync_machine b sync)"
sync_machine a sync > /dev/null

DA="$(dump a)"; DB="$(dump b)"
assert_contains "newer edit (B) won on B"        "$DB" "edited on B"
assert_not_contains "older edit (A) dropped on B" "$DB" "edited on A"
assert_contains "newer edit propagated to A"     "$DA" "edited on B"
assert_eq       "machines converged"             "$DA" "$DB"
assert_contains "conflict was reported"          "$OUT_B" "conflict"

head_ "Scenario 3: delete on one side, edit on the other - the edit survives"
fresh_pair
$FIXTURE del-lesson "$WORK/a" lesson.doomed
$FIXTURE set-lesson "$WORK/b" lesson.doomed "revived on B" --ts 2026-03-02T00:00:00+00:00

sync_machine a sync > /dev/null
sync_machine b sync > /dev/null
sync_machine a sync > /dev/null

DA="$(dump a)"; DB="$(dump b)"
assert_contains "edit beat the delete on A" "$DA" "revived on B"
assert_eq       "machines converged"        "$DA" "$DB"

head_ "Scenario 4: plain delete propagates when the other side did not touch it"
fresh_pair
$FIXTURE del-lesson "$WORK/a" lesson.doomed
sync_machine a sync > /dev/null
sync_machine b sync > /dev/null

DB="$(dump b)"
assert_not_contains "deletion reached B" "$DB" "lesson.doomed"

head_ "Scenario 5: --strategy local-wins and remote-wins"
fresh_pair
$FIXTURE set-lesson "$WORK/a" lesson.conflict "A version" --ts 2026-03-01T09:00:00+00:00
$FIXTURE set-lesson "$WORK/b" lesson.conflict "B version" --ts 2026-03-01T18:00:00+00:00
sync_machine a sync > /dev/null
sync_machine b sync --strategy local-wins > /dev/null

DB="$(dump b)"
assert_contains "local-wins kept B's version"  "$DB" "B version"

fresh_pair
$FIXTURE set-lesson "$WORK/a" lesson.conflict "A version" --ts 2026-03-01T18:00:00+00:00
$FIXTURE set-lesson "$WORK/b" lesson.conflict "B version" --ts 2026-03-01T09:00:00+00:00
sync_machine a sync > /dev/null
sync_machine b sync --strategy remote-wins > /dev/null

DB="$(dump b)"
assert_contains "remote-wins took A's version" "$DB" "A version"

head_ "Scenario 6: machine-local state and secrets never cross"
fresh_pair
$FIXTURE set-lesson "$WORK/a" lesson.x "x" --ts 2026-03-01T10:00:00+00:00
sync_machine a sync > /dev/null
sync_machine b sync > /dev/null

LA="$(local_only a)"; LB="$(local_only b)"
# ingestion_jobs is transient per-machine job state and never travels.
assert_contains     "A kept its own job row"        "$LA" "job-a"
assert_not_contains "A did not receive B's job row" "$LA" "job-b"
assert_contains     "B kept its own job row"        "$LB" "job-b"
assert_not_contains "B did not receive A's job row" "$LB" "job-a"
# folder_file_state does travel: it links scanned files to the items built
# from them, so dropping it would make the other machine re-ingest and
# re-embed a folder it already has. Paths are translated on the way (ADR 0001).
assert_contains     "A kept its own folder path"    "$LA" "/machine/a/local/path"
assert_contains     "A received B's folder state"   "$LA" "/machine/b/local/path"
assert_contains     "B received A's folder state"   "$LB" "/machine/a/local/path"
# Credentials stay put regardless.
assert_contains     "A kept its own bot token"      "$LA" "SECRET-a"
assert_contains     "B kept its own bot token"      "$LB" "SECRET-b"

REMOTE_SCAN="$(find "$WORK/remote" -type f -exec cat {} + 2>/dev/null | strings 2>/dev/null || true)"
assert_not_contains "no bot token in published data" "$REMOTE_SCAN" "SECRET-a"
assert_not_contains "no mcp token in published data" "$REMOTE_SCAN" "live-token"
assert_not_contains "no local secret published"      "$REMOTE_SCAN" "topsecret"

head_ "Scenario 7: config.json merges key by key"
fresh_pair
$FIXTURE set-config "$WORK/a" default_agent "agent-from-a"
$FIXTURE set-config "$WORK/b" session.limit 99
sync_machine a sync > /dev/null
sync_machine b sync > /dev/null
sync_machine a sync > /dev/null

DA="$(dump a)"; DB="$(dump b)"
assert_contains "A's key reached B"        "$DB" "agent-from-a"
assert_contains "B's key stayed on B"      "$DB" "99"
assert_contains "B's key reached A"        "$DA" "99"

head_ "Scenario 8: session transcripts union, FTS rebuilt, event log renumbered"
fresh_pair
$FIXTURE set-lesson "$WORK/a" lesson.e1 "e1" --ts 2026-03-01T10:00:00+00:00
$FIXTURE set-lesson "$WORK/b" lesson.e2 "e2" --ts 2026-03-01T11:00:00+00:00
$FIXTURE add-item   "$WORK/a" item-fts-a "Searchable A" --ts 2026-03-01T10:00:00+00:00
$FIXTURE add-item   "$WORK/b" item-fts-b "Searchable B" --ts 2026-03-01T11:00:00+00:00
sync_machine a sync > /dev/null
sync_machine b sync > /dev/null
sync_machine a sync > /dev/null

DA="$(dump a)"; DB="$(dump b)"
assert_contains "A has B's session file"  "$DA" "chat-b.jsonl"
assert_contains "B has A's session file"  "$DB" "chat-a.jsonl"
assert_eq       "machines converged"      "$DA" "$DB"

# The FTS index is derived data: it is never merged, only rebuilt after pack.
# It must therefore index rows that arrived from the other machine.
HITS_A="$(dump a | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin)["fts_hits"]))')"
HITS_B="$(dump b | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin)["fts_hits"]))')"
assert_contains "A's FTS indexes B's item" "$HITS_A" "item-fts-b"
assert_contains "B's FTS indexes A's item" "$HITS_B" "item-fts-a"
assert_contains "A's FTS still has its own" "$HITS_A" "item-fts-a"

IDS_A="$(dump a | python3 -c 'import json,sys; print(json.load(sys.stdin)["event_ids"])')"
IDS_B="$(dump b | python3 -c 'import json,sys; print(json.load(sys.stdin)["event_ids"])')"
EXPECTED="$(python3 -c 'import sys; n=int(sys.argv[1]); print(list(range(1,n+1)))' \
    "$(echo "$IDS_A" | tr ',' '\n' | wc -l)")"
assert_eq "event ids are contiguous after renumbering" "$IDS_A" "$EXPECTED"
assert_eq "event ids match across machines"            "$IDS_A" "$IDS_B"

head_ "Scenario 9: repeated sync with no changes is a no-op (convergence)"
BEFORE="$(dump a)"
sync_machine a sync > /dev/null
sync_machine b sync > /dev/null
sync_machine a sync > /dev/null
AFTER="$(dump a)"
assert_eq "idle syncs change nothing" "$BEFORE" "$AFTER"

REPO="$WORK/a/.sync/repo"
NEW_COMMITS="$(git -C "$REPO" log --oneline "$(git -C "$REPO" rev-list -n1 --skip=0 main)" 2>/dev/null | wc -l)"
CLEAN="$(git -C "$REPO" status --porcelain | wc -l | tr -d ' ')"
assert_eq "working tree clean after idle sync" "$CLEAN" "0"

head_ "Scenario 10: mismatched embedding models are refused"
rm -rf "$WORK/remote"
setup_machine a sig-model-one
setup_machine b sig-model-two
sync_machine a sync > /dev/null 2>&1
OUT="$(sync_machine b sync 2>&1)"
assert_contains "embedding mismatch detected" "$OUT" "embedding space mismatch"
assert_contains "sync refused to merge"                "$OUT" "incompatible machine"

head_ "Scenario 11: dry run writes nothing"
fresh_pair
$FIXTURE set-lesson "$WORK/a" lesson.dry "dry" --ts 2026-03-01T10:00:00+00:00
sync_machine a sync > /dev/null
BEFORE="$(dump b)"
OUT="$(sync_machine b sync --dry-run)"
AFTER="$(dump b)"
assert_eq       "dry run left data untouched" "$BEFORE" "$AFTER"
assert_contains "dry run said so"             "$OUT" "Dry run complete"

# --------------------------------------------------------------------------

echo
echo "-----------------------------------------"
echo -e "  ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}"
echo "  workdir: $WORK"
echo "-----------------------------------------"
[ "$FAIL" -eq 0 ]
