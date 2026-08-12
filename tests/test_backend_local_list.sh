#!/usr/bin/env bash
#
# backend_list() is the cheap remote-state fingerprint lib/daemon.sh's
# check_remote_changed() polls on every daemon tick (as often as every 30s)
# to decide whether a full sync is worth running. Before this test existed,
# backend_list was never defined anywhere in the repo, so the daemon's
# change-detection path was permanently dead (see tests/test_daemon.sh,
# Test 8, and lib/daemon.sh:251).
#
# This exercises the one backend testable without real credentials or a
# real remote: backends/local.sh. It proves the contract the other three
# backends (gdrive.sh, s3.sh, rsync.sh) also implement:
#   - reachable + no bundles published yet -> the literal sentinel "EMPTY"
#     (never the empty string -- that's reserved for "unreachable", per
#     check_remote_changed()'s own `[ -z "$current_state" ]` check)
#   - reachable + bundles present -> a sorted "name size mtime" listing
#   - unreachable (here: the shared-folder root itself doesn't exist) ->
#     no stdout and a non-zero exit
#   - the same remote state, queried twice, must fingerprint identically --
#     this is the exact property check_remote_changed() relies on to tell
#     "changed" from "unchanged" instead of flip-flopping on every poll.
#
# backend_list() only touches bash builtins, dirname/basename/stat/sort --
# no log_*  helpers -- so backends/local.sh can be sourced standalone here,
# without pulling in the rest of kirocrew-sync.sh.
#
# Run: ./tests/test_backend_local_list.sh
#

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0

ok()  { printf '  \033[0;32m✓\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf '  \033[0;31m✗\033[0m %s\n' "$1"; shift; for line in "$@"; do printf '      %s\n' "$line"; done; FAIL=$((FAIL + 1)); }

assert_eq() {
    local name="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then ok "$name"
    else bad "$name" "expected: $expected" "actual:   $actual"; fi
}

echo "backends/local.sh: backend_list() fingerprint contract"
echo

# shellcheck source=/dev/null
source "$REPO_DIR/backends/local.sh"

# --- Scenario 1: reachable, nothing published yet ---------------------------
# LOCAL_SYNC_DIR's parent exists (the "shared folder root" is there) but
# LOCAL_SYNC_DIR itself, and its bundles/ subdir, do not -- exactly the
# state before any machine has ever pushed.
SHARED_ROOT="$WORK/shared-root"
mkdir -p "$SHARED_ROOT"
export LOCAL_SYNC_DIR="$SHARED_ROOT/KiroCrew-Sync"

out=$(backend_list); rc=$?
assert_eq "empty-but-reachable remote exits 0" "0" "$rc"
assert_eq "empty-but-reachable remote prints the EMPTY sentinel" "EMPTY" "$out"

# --- Scenario 2: a published bundle ------------------------------------------
mkdir -p "$LOCAL_SYNC_DIR/bundles"
echo "fake bundle content" > "$LOCAL_SYNC_DIR/bundles/machine-a.bundle"

first=$(backend_list); rc=$?
assert_eq "one published bundle exits 0" "0" "$rc"
case "$first" in
    *"machine-a.bundle"*) ok "listing names the published bundle" ;;
    *) bad "listing names the published bundle" "got: $first" ;;
esac
case "$first" in
    "EMPTY") bad "listing is not the EMPTY sentinel once a bundle exists" ;;
    *) ok "listing is not the EMPTY sentinel once a bundle exists" ;;
esac

# --- Scenario 3: a stable remote fingerprints identically across two calls --
# The property check_remote_changed() actually depends on: querying twice
# in a row with nothing published in between must not, by itself, look
# like a change (no query-timestamp, no unstable ordering).
second=$(backend_list)
assert_eq "an unchanged remote fingerprints identically across two calls" \
    "$first" "$second"

# --- Scenario 4: a second published bundle changes the fingerprint ----------
# mtimes have only 1-second resolution on most filesystems; without a
# forced gap two bundles written in the same wall-clock second could get
# an identical mtime, and only the (still-included) size/name would carry
# the change. Sleep briefly to also exercise the mtime component directly.
sleep 1.1
echo "different fake content, and longer" > "$LOCAL_SYNC_DIR/bundles/machine-b.bundle"

third=$(backend_list); rc=$?
assert_eq "second published bundle exits 0" "0" "$rc"
if [ "$third" != "$first" ]; then
    ok "publishing a second bundle changes the fingerprint"
else
    bad "publishing a second bundle changes the fingerprint" "fingerprint unchanged: $third"
fi
case "$third" in
    *"machine-b.bundle"*) ok "listing names the second published bundle" ;;
    *) bad "listing names the second published bundle" "got: $third" ;;
esac

# Sorted deterministically: bundle lines must appear in a fixed (sorted)
# order regardless of directory-entry/creation order, so re-querying an
# unchanged remote can never look changed merely from enumeration order.
sorted_expected=$(printf '%s\n%s' "machine-a.bundle" "machine-b.bundle" | LC_ALL=C sort)
names_only=$(printf '%s\n' "$third" | awk '{print $1}')
assert_eq "bundle lines are sorted by name" "$sorted_expected" "$names_only"

# --- Scenario 5: unreachable remote (shared folder root itself is gone) ----
# Distinct from Scenario 1: there the root existed and only the sync dir
# was absent (reachable, empty). Here the root itself -- the thing
# check_local_configured() requires -- is gone, e.g. an unmounted Dropbox
# folder or disconnected drive.
export LOCAL_SYNC_DIR="$WORK/does-not-exist/KiroCrew-Sync"

out=$(backend_list); rc=$?
assert_eq "unreachable remote exits non-zero" "1" "$rc"
assert_eq "unreachable remote prints nothing" "" "$out"

echo
if [ "$FAIL" -gt 0 ]; then
    printf '\033[0;31m✗\033[0m %d passed, %d failed\n' "$PASS" "$FAIL"
    exit 1
fi
printf '\033[0;32m✓\033[0m %d passed\n' "$PASS"
