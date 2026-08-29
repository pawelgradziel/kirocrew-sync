#!/usr/bin/env bash
#
# export / import: the seeding primitive named as a follow-up in ADR 0002
# ("Snapshot + restore as a KiroCrew subcommand"). Covers: a fresh machine
# seeded from another's export converges with the existing pair and needs no
# real merge on its first ordinary sync -- not "merges cleanly", but
# "nothing to merge", because the archive carries real git ancestry rather
# than a bare state tarball; import refuses to touch a machine that already
# has local state unless told --force, and --force does exactly what the
# refusal said it would; scope and embedding-space mismatches are refused
# outright, with no --force override, because unlike sync's per-machine
# quarantine there is only one input to get right; there is no merge mode;
# and the archive itself never carries a credential -- verified by unpacking
# it and reading the objects, not by grepping the compressed bytes directly
# (see run_tests.sh's published_dump() comment for why that check would be
# vacuous: a git bundle is a packfile, so a raw grep finds no plaintext
# whether the secret is in there or not).
#
# Usage: tests/test_seed.sh [workdir]
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

# See run_tests.sh for why these use a here-string rather than a pipe: under
# `set -o pipefail` (line below), `echo "$2" | grep -q ...` can race SIGPIPE
# and report "not found" for a pattern that is genuinely there. A here-string
# is fully materialized before grep ever starts reading, so there is no
# concurrent writer left to race.
assert_contains() {
    if grep -aqF -- "$3" <<< "$2"; then ok "$1"; else
        bad "$1"
        echo "      expected to find: $3"
    fi
}
assert_not_contains() {
    if grep -aqF -- "$3" <<< "$2"; then
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

# A freshly-installed KiroCrew: real schema, zero content rows. Distinct from
# setup_machine, whose fixture.py `create` seeds baseline shared rows so
# ordinary merge scenarios have a real base -- that content would make every
# import below need --force regardless of what it is actually testing, which
# defeats the point of the fresh-machine scenarios.
setup_fresh_machine() {
    local name="$1" sig="${2:-sig-shared}"
    local dir="$WORK/$name"
    rm -rf "$dir"
    mkdir -p "$dir"
    $FIXTURE create-empty "$dir" --name "$name" --embedding-sig "$sig" > /dev/null
    echo "machine-$name" > "$dir/.machine_id"
    cat > "$WORK/config-$name.sh" << EOF
export SYNC_BACKEND="local"
export KIROCREW_DIR="$dir"
export LOCAL_SYNC_DIR="$WORK/remote"
EOF
}

run() {
    local name="$1"; shift
    KIROCREW_SYNC_CONFIG="$WORK/config-$name.sh" \
    KIROCREW_SYNC_SKIP_RUNNING_CHECK=1 \
        "$ROOT/kirocrew-sync.sh" "$@" 2>&1
}

dump() { $FIXTURE dump "$WORK/$1"; }

# Everything inside an export archive, as plain text -- unpacked, not grepped
# straight off the compressed bytes. A git bundle is a packfile, exactly like
# the transport bundles run_tests.sh's published_dump() reads, so grepping
# the .tar.gz or the .bundle file directly would prove nothing.
archive_dump() {
    local archive="$1"
    local tmp
    tmp="$(mktemp -d)"
    tar -xzf "$archive" -C "$tmp" 2>/dev/null
    cat "$tmp/manifest.json" 2>/dev/null
    if [ -f "$tmp/repo.bundle" ]; then
        git init -q "$tmp/git"
        git -C "$tmp/git" fetch -q "$tmp/repo.bundle" \
            "refs/heads/*:refs/remotes/seed/*" 2>/dev/null || true
        git -C "$tmp/git" rev-list --all --objects 2>/dev/null \
            | awk 'NF > 1 {print $2}' | sort -u
        git -C "$tmp/git" cat-file --batch-all-objects --batch 2>/dev/null \
            | tr -d '\000'
    fi
    rm -rf "$tmp"
}

# --------------------------------------------------------------------------

echo "workdir: $WORK"

head_ "Scenario 1: export archives current state, and carries no secret"
rm -rf "$WORK/remote"
setup_machine a
setup_machine b
run a sync > /dev/null 2>&1
run b sync > /dev/null 2>&1
run a sync > /dev/null 2>&1

OUT="$(run a export -o "$WORK/a.tar.gz")"; RC=$?
assert_eq       "export exits 0"                            "$RC" "0"
assert_contains "export reports where it wrote the archive" "$OUT" "$WORK/a.tar.gz"
[ -f "$WORK/a.tar.gz" ] && ok "archive file was created" || bad "archive file was created"

SCAN="$(archive_dump "$WORK/a.tar.gz")"
assert_contains     "scan is non-vacuous (finds ordinary content)" "$SCAN" "default_agent"
assert_contains     "manifest records the scope"                   "$SCAN" '"scope": "personal"'
assert_contains     "manifest records the machine id"              "$SCAN" '"machine_id": "machine-a"'
assert_contains     "manifest records the embedding signature"     "$SCAN" '"embedding_space_sig": "sig-shared"'
assert_not_contains "no bot token in the archive"                  "$SCAN" "SECRET-a"
assert_not_contains "no mcp token in the archive"                  "$SCAN" "live-token"
assert_not_contains "no local secret in the archive"               "$SCAN" "topsecret"
assert_not_contains "no nested .git credential in the archive"     "$SCAN" "ghp-gitsecret"

head_ "Scenario 2: a fresh machine imports and converges, ancestry intact"
setup_fresh_machine c
OUT="$(run c import "$WORK/a.tar.gz")"; RC=$?
assert_eq       "import onto a fresh machine exits 0 without --force" "$RC" "0"
assert_contains "import reports success"                              "$OUT" "Import complete"

# The point of carrying git history rather than a bare tarball: C already has
# A's exact state as an ancestor, so its first ordinary sync needs no real
# merge at all -- not "merges cleanly", but "nothing to merge".
run a sync > /dev/null 2>&1
SYNC_OUT="$(run c sync)"; SYNC_RC=$?
assert_eq           "C's first sync after import exits 0"       "$SYNC_RC" "0"
assert_not_contains "no conflicts on rows the seed already carried" \
    "$SYNC_OUT" "conflict"
run a sync > /dev/null 2>&1

assert_eq "C converges with A after the seed" "$(dump a)" "$(dump c)"

# The pair still works normally afterwards: B, who never heard of the
# import, still merges cleanly with both A and the newly-seeded C.
run b sync > /dev/null 2>&1
run a sync > /dev/null 2>&1
run c sync > /dev/null 2>&1
assert_eq "A and B still converge" "$(dump a)" "$(dump b)"
assert_eq "B and C converge too"   "$(dump b)" "$(dump c)"

head_ "Scenario 3: import onto existing state refuses without --force, honours it with --force"
setup_machine d
BEFORE="$(dump d)"
OUT="$(run d import "$WORK/a.tar.gz")"; RC=$?
assert_eq       "refused without --force"                    "$RC" "1"
assert_contains "says local state already exists"            "$OUT" "already has local state"
assert_contains "names the row counts it would replace"      "$OUT" "synced row(s)"
assert_contains "names that local-only database rows are deleted" \
    "$OUT" "DELETED"
assert_contains "names that credentials are kept, not overwritten" \
    "$OUT" "credential-shaped"
assert_contains "points at --force"                          "$OUT" "Re-run with --force"
AFTER="$(dump d)"
assert_eq "local data untouched by the refused attempt" "$BEFORE" "$AFTER"

OUT="$(run d import "$WORK/a.tar.gz" --force)"; RC=$?
assert_eq       "--force proceeds"                              "$RC" "0"
assert_contains "--force reports success"                       "$OUT" "Import complete"
assert_contains "backs up before writing, like any other pack"  "$OUT" "backed up"

head_ "Scenario 4: scope mismatch is refused outright, no --force override"
setup_fresh_machine e
OUT="$(run e import "$WORK/a.tar.gz" --team)"; RC=$?
assert_eq       "scope-mismatched import fails" "$RC" "1"
assert_contains "names the mismatch"            "$OUT" "scope mismatch"

OUT="$(run e import "$WORK/a.tar.gz" --team --force)"; RC=$?
assert_eq       "scope mismatch is not overridable by --force" "$RC" "1"
assert_contains "still names the mismatch"                     "$OUT" "scope mismatch"

head_ "Scenario 5: embedding-space mismatch against existing local data is refused"
# f has no content rows, but it does have a real local embedding_space_sig --
# exactly the case the gate has to catch: "local state already exists" means
# the machine's own embedding model is established, not that it has content.
setup_fresh_machine f sig-model-two
OUT="$(run f import "$WORK/a.tar.gz")"; RC=$?
assert_eq       "embedding-mismatched import fails" "$RC" "1"
assert_contains "names the embedding mismatch"      "$OUT" "embedding space mismatch"

OUT="$(run f import "$WORK/a.tar.gz" --force)"; RC=$?
assert_eq       "embedding mismatch is not overridable by --force either" "$RC" "1"
assert_contains "still names the embedding mismatch"                     "$OUT" "embedding space mismatch"

head_ "Scenario 6: there is no merge mode"
setup_fresh_machine g
OUT="$(run g import "$WORK/a.tar.gz" --mode merge)"; RC=$?
assert_eq       "--mode merge is refused"         "$RC" "1"
assert_contains "explains there is no merge mode" "$OUT" "no 'merge' mode"
assert_contains "points at sync instead"          "$OUT" "'sync' does"
if [ -d "$WORK/g/.sync/repo/.git" ]; then
    bad "nothing was materialized by the refused --mode merge"
else
    ok "nothing was materialized by the refused --mode merge"
fi

OUT="$(run g import "$WORK/a.tar.gz" --mode replace)"; RC=$?
assert_eq       "--mode replace is accepted (it's the only real mode, and the default)" "$RC" "0"
assert_contains "and it actually imports" "$OUT" "Import complete"

# --------------------------------------------------------------------------

echo
echo "-----------------------------------------"
echo -e "  ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}"
echo "  workdir: $WORK"
echo "-----------------------------------------"
[ "$FAIL" -eq 0 ]
