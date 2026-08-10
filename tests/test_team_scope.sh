#!/usr/bin/env bash
#
# Team scope: two colleagues sharing one knowledge library.
#
# The question every assertion here answers is the same one: did something
# personal reach the other person? Checking the receiving machine is not
# enough on its own -- data can be withheld from the merge and still sit in
# the published bundle -- so the important assertions read the bundles.
#
# Usage: tests/test_team_scope.sh [workdir]
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

assert_contains() {
    if echo "$2" | grep -qF -- "$3"; then ok "$1"; else
        bad "$1"; echo "      expected to find: $3"
    fi
}
assert_not_contains() {
    if echo "$2" | grep -qF -- "$3"; then
        bad "$1"; echo "      should not contain: $3"
    else ok "$1"; fi
}
assert_eq() {
    if [ "$2" = "$3" ]; then ok "$1"; else
        bad "$1"; echo "      left:  $2"; echo "      right: $3"
    fi
}

# --------------------------------------------------------------------------

setup_person() {
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

sync_as() {
    local name="$1"; shift
    KIROCREW_SYNC_CONFIG="$WORK/config-$name.sh" \
    KIROCREW_SYNC_SKIP_RUNNING_CHECK=1 \
        "$ROOT/kirocrew-sync.sh" "$@" 2>&1
}

dump()     { $FIXTURE dump "$WORK/$1"; }
episodic() { $FIXTURE episodic "$WORK/$1"; }

# Everything that actually reached the backend, as plain text. Bundles are
# packfiles, so grepping them directly finds no plaintext whether a secret is
# in there or not -- unpack first, then read every object.
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

# --------------------------------------------------------------------------

echo "workdir: $WORK"

head_ "Scenario 1: colleagues share the library, not their transcripts"
rm -rf "$WORK/remote"
setup_person alice
setup_person bob
sync_as alice sync --team > /dev/null 2>&1
sync_as bob   sync --team > /dev/null 2>&1

$FIXTURE set-lesson "$WORK/alice" lesson.from-alice "alice's lesson" --ts 2026-03-01T10:00:00+00:00
$FIXTURE add-item   "$WORK/alice" item-alice "Alice's document" --ts 2026-03-01T10:00:00+00:00
$FIXTURE set-config "$WORK/alice" default_agent "alices-private-agent"
$FIXTURE set-lesson "$WORK/bob"   lesson.from-bob "bob's lesson" --ts 2026-03-01T11:00:00+00:00

for r in 1 2; do
    sync_as alice sync --team > /dev/null 2>&1
    sync_as bob   sync --team > /dev/null 2>&1
done

DB="$(dump bob)"; DA="$(dump alice)"
assert_contains "knowledge item reached the colleague"  "$DB" "item-alice"
assert_contains "learned lesson reached the colleague"  "$DB" "lesson.from-alice"
assert_contains "and it works the other way"            "$DA" "lesson.from-bob"

# The whole point of the scope: these stay home.
assert_not_contains "chat transcript did not cross" "$DB" "chat-alice.jsonl"
assert_not_contains "personal config did not cross" "$DB" "alices-private-agent"
assert_not_contains "episodic memory did not cross" "$(episodic bob)" \
    "private conversation on alice"

head_ "Scenario 2: nothing personal is in the published bundles"
# Withholding data from the merge is not the same as never publishing it. A
# colleague who reads the backend directly must not find it either.
SCAN="$(published_dump)"
assert_contains     "scan is non-vacuous"          "$SCAN" "item-alice"
assert_contains     "shared lesson was published"  "$SCAN" "lesson.from-alice"
assert_not_contains "no transcript published"      "$SCAN" "chat-alice.jsonl"
assert_not_contains "no transcript body published" "$SCAN" "hello from alice"
assert_not_contains "no episodic memory published" "$SCAN" "private conversation on alice"
assert_not_contains "no personal config published" "$SCAN" "alices-private-agent"
assert_not_contains "no memory event log published" "$SCAN" "memory_events.jsonl"
assert_not_contains "no per-machine ingest state"  "$SCAN" "folder_file_state.jsonl"
assert_not_contains "no credential published"      "$SCAN" "SECRET-alice"

head_ "Scenario 3: team scope leaves personal data on the machine intact"
# Packing a team repo must not delete the rows it never carried.
assert_contains "alice still has her own transcript" \
    "$(ls "$WORK/alice/sessions")" "chat-alice.jsonl"
assert_contains "alice still has her episodic memory" \
    "$(episodic alice)" "private conversation on alice"
assert_contains "alice still has her own config" \
    "$(cat "$WORK/alice/config.json")" "alices-private-agent"

head_ "Scenario 4: repeated team syncs converge and stay quiet"
BEFORE="$(dump alice)"
sync_as alice sync --team > /dev/null 2>&1
sync_as bob   sync --team > /dev/null 2>&1
sync_as alice sync --team > /dev/null; RC=$?
AFTER="$(dump alice)"
assert_eq "idle team syncs change nothing" "$BEFORE" "$AFTER"
assert_eq "successful team sync exits 0"   "$RC" "0"
CLEAN="$(git -C "$WORK/alice/.sync/repo-team" status --porcelain | wc -l | tr -d ' ')"
assert_eq "team working tree clean"        "$CLEAN" "0"

head_ "Scenario 5: a personal-scope machine is quarantined, not merged"
# Forgetting --team once must not publish a transcript into the team's history.
rm -rf "$WORK/remote"
setup_person alice
setup_person bob
sync_as alice sync --team > /dev/null 2>&1
OUT="$(sync_as bob sync 2>&1)"; RC=$?          # bob forgets --team
assert_contains "scope mismatch detected" "$OUT" "scope mismatch"
assert_contains "machine was quarantined" "$OUT" "quarantined"
assert_eq       "exit code says partial"  "$RC" "3"
assert_not_contains "team data did not leak into the personal repo" \
    "$(dump bob)" "item-alice"

head_ "Scenario 6: the two scopes keep separate repos"
rm -rf "$WORK/remote"
setup_person alice
sync_as alice sync --team > /dev/null 2>&1
rm -rf "$WORK/remote"
sync_as alice sync > /dev/null 2>&1
assert_eq "team repo exists" \
    "$([ -d "$WORK/alice/.sync/repo-team/.git" ] && echo yes || echo no)" "yes"
assert_eq "personal repo exists" \
    "$([ -d "$WORK/alice/.sync/repo/.git" ] && echo yes || echo no)" "yes"
# A team repo must never contain the files the personal one carries, or
# switching scope would look like a mass deletion and propagate as one.
assert_eq "team repo carries no transcripts" \
    "$(ls "$WORK/alice/.sync/repo-team/files/sessions" 2>/dev/null | wc -l | tr -d ' ')" "0"
assert_contains "personal repo does carry them" \
    "$(ls "$WORK/alice/.sync/repo/files/sessions" 2>/dev/null)" "chat-alice.jsonl"

# --------------------------------------------------------------------------

echo
echo "-----------------------------------------"
echo -e "  ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}"
echo "  workdir: $WORK"
echo "-----------------------------------------"
[ "$FAIL" -eq 0 ]
