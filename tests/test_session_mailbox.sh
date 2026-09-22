#!/usr/bin/env bash
#
# Session mailbox: send-session and inbox, end to end.
#
# Two throwaway KiroCrew directories (alice, bob) share one local-directory
# backend. Each has its own fake gateway (tests/fake_gateway.py), which
# implements the four routes the mailbox uses: health, the local token mint,
# the session export, and the session import. The questions:
#
#   - does a session get from one gateway's export to the other's import,
#     with the sender named so upstream files it under "from <sender>"?
#   - is it installed exactly once, however often inbox runs, and however
#     many names the same bytes arrive under?
#   - does mailbox traffic stay out of the three-way sync entirely, and
#     survive a sync's publish (which mirrors with delete)?
#   - is team scope explicit, and are "gateway down" and "no credential"
#     reported as such instead of as a generic failure?
#
# Usage: tests/test_session_mailbox.sh [workdir]
#

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$TEST_DIR/.." && pwd)"
WORK="${1:-$(mktemp -d)}"

FIXTURE="python3 $TEST_DIR/fixture.py"
PASS=0
FAIL=0
GATEWAY_PIDS=()

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

ok()    { echo -e "  ${GREEN}PASS${NC} $*"; PASS=$((PASS + 1)); }
bad()   { echo -e "  ${RED}FAIL${NC} $*"; FAIL=$((FAIL + 1)); }
head_() { echo; echo -e "${YELLOW}== $*${NC}"; }

# Here-strings, not `echo | grep -q`: see the SIGPIPE note in run_tests.sh.
assert_contains() {
    if grep -qF -- "$3" <<< "$2"; then ok "$1"; else
        bad "$1"; echo "      expected to find: $3"
        echo "$2" | sed 's/^/      | /' | head -20
    fi
}
assert_not_contains() {
    if grep -qF -- "$3" <<< "$2"; then
        bad "$1"; echo "      should not contain: $3"
    else ok "$1"; fi
}
assert_eq() {
    if [ "$2" = "$3" ]; then ok "$1"; else
        bad "$1"; echo "      left:  $2"; echo "      right: $3"
    fi
}

cleanup() {
    local pid
    for pid in ${GATEWAY_PIDS[@]+"${GATEWAY_PIDS[@]}"}; do
        kill "$pid" 2>/dev/null || true
    done
}
trap cleanup EXIT

# --------------------------------------------------------------------------

cat > "$WORK/slots.json" << 'EOF'
{
  "slot-1": {
    "title": "Deploy notes",
    "messages": [
      {"role": "user", "content": "how do we deploy the api?", "ts": "2026-09-22T10:00:00Z"},
      {"role": "assistant", "content": "run make deploy from the api dir", "ts": "2026-09-22T10:00:05Z"}
    ],
    "layer_b": {"envelope": {"session_id": "sid-1", "cwd": "/home/alice/api"},
                "events": "{\"kind\":\"turn\",\"signature\":\"abc\"}\n"}
  },
  "slot-2": {
    "title": "Weekly plan",
    "messages": [
      {"role": "user", "content": "plan the week", "ts": ""},
      {"role": "assistant", "content": "monday: reviews", "ts": ""}
    ]
  },
  "slot-3": {
    "title": "Broadcast me",
    "messages": [{"role": "user", "content": "for all my machines", "ts": ""}]
  },
  "slot-4": {
    "title": "Late arrival",
    "messages": [{"role": "user", "content": "sent while bob was offline", "ts": ""}]
  }
}
EOF

setup_machine() {
    local name="$1" dir="$WORK/$1"
    rm -rf "$dir"
    mkdir -p "$dir"
    $FIXTURE create "$dir" --name "$name" > /dev/null
    echo "machine-$name" > "$dir/.machine_id"
}

write_config() {
    local name="$1" port="$2"
    cat > "$WORK/config-$name.sh" << EOF
export SYNC_BACKEND="local"
export KIROCREW_DIR="$WORK/$name"
export LOCAL_SYNC_DIR="$WORK/remote"
export MAILBOX_NAME="$name"
export KIROCREW_PORT="$port"
EOF
}

# Starts a fake gateway for <name> over TCP; its port lands in
# $WORK/gw-<name>/port. Not called from $(...): a background child would
# hold the substitution's pipe open, and its PID would be lost with the
# subshell.
start_gateway() {
    local name="$1" state="$WORK/gw-$1"
    mkdir -p "$state"
    rm -f "$state/port"
    python3 "$TEST_DIR/fake_gateway.py" --secret "$(cat "$WORK/$name/.local_secret")" \
        --state "$state" --slots "$WORK/slots.json" --port-file "$state/port" \
        > "$state/log" 2>&1 &
    GATEWAY_PIDS+=($!)
    local i
    for i in $(seq 1 50); do
        [ -s "$state/port" ] && break
        sleep 0.1
    done
}

as() {
    local name="$1"; shift
    KIROCREW_SYNC_CONFIG="$WORK/config-$name.sh" \
    KIROCREW_SYNC_SKIP_RUNNING_CHECK=1 \
        "$ROOT/kirocrew-sync.sh" "$@" 2>&1
}

mailbox_files() { find "$WORK/remote/mailbox" -name '*.kcsession.json.gz' 2>/dev/null | sort; }
imports_of()    { cat "$WORK/gw-$1/imports.jsonl" 2>/dev/null; }
import_count()  { imports_of "$1" | grep -c . || true; }
bundle_field()  { python3 -c '
import gzip, json, sys
b = json.loads(gzip.open(sys.argv[1]).read())
v = b.get(sys.argv[2])
print(json.dumps(v) if not isinstance(v, str) else v)' "$1" "$2"; }

# Everything the sync published, as a list of paths across all history.
published_paths() {
    local tmp bundle
    tmp="$(mktemp -d)"
    git init -q "$tmp"
    for bundle in "$WORK"/remote/bundles/*.bundle; do
        [ -f "$bundle" ] || continue
        git -C "$tmp" fetch -q "$bundle" \
            "refs/heads/*:refs/remotes/$(basename "$bundle" .bundle)/*" 2>/dev/null || true
    done
    git -C "$tmp" log --all --name-only --format= 2>/dev/null | sort -u
    rm -rf "$tmp"
}

setup_machine alice
setup_machine bob
start_gateway alice
start_gateway bob
PORT_A="$(cat "$WORK/gw-alice/port" 2>/dev/null)"
PORT_B="$(cat "$WORK/gw-bob/port" 2>/dev/null)"
write_config alice "$PORT_A"
write_config bob "$PORT_B"

if [ -z "$PORT_A" ] || [ -z "$PORT_B" ]; then
    echo "fake gateways did not start"; exit 1
fi

# --------------------------------------------------------------------------
head_ "1. Failing cleanly: no gateway, no credential, refused credential"

# A port nothing listens on: bind one, close it.
DEAD_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
write_config alice "$DEAD_PORT"
out="$(as alice send-session slot-1 --to bob)"; rc=$?
assert_eq "send-session with no gateway exits 1" "1" "$rc"
assert_contains "and says KiroCrew is not running" "$out" "KiroCrew is not running"
assert_contains "and names the port it tried" "$out" "127.0.0.1:$DEAD_PORT"
assert_contains "and says nothing was sent" "$out" "Nothing was sent"
assert_eq "nothing reached the mailbox" "" "$(mailbox_files)"
write_config alice "$PORT_A"

mv "$WORK/alice/.local_secret" "$WORK/alice/.local_secret.off"
out="$(as alice send-session slot-1 --to bob)"; rc=$?
assert_eq "send-session with no readable secret exits 1" "1" "$rc"
assert_contains "and says there is no gateway credential" "$out" "No gateway credential"
mv "$WORK/alice/.local_secret.off" "$WORK/alice/.local_secret"

# The per-listener secret wins over the shared one (upstream read_local_secret
# order). A wrong one there must be refused, and reported as a refusal.
mkdir -p "$WORK/alice/run"
echo "not-the-secret" > "$WORK/alice/run/gateway-$PORT_A.secret"
out="$(as alice send-session slot-1 --to bob)"; rc=$?
assert_eq "a wrong per-gateway secret exits 1" "1" "$rc"
assert_contains "and reports the gateway's refusal" "$out" "refused a local token (HTTP 403): invalid secret"
rm -f "$WORK/alice/run/gateway-$PORT_A.secret"
assert_eq "still nothing in the mailbox" "" "$(mailbox_files)"

out="$(as alice send-session nope --to bob)"; rc=$?
assert_eq "an unknown slot exits 1" "1" "$rc"
assert_contains "and passes on KiroCrew's 404" "$out" "export_slot_not_found"
assert_contains "with a hint about loaded sessions" "$out" "currently has loaded"

out="$(as alice send-session)"; rc=$?
assert_eq "send-session without a slot exits 1" "1" "$rc"

# --------------------------------------------------------------------------
head_ "2. send-session: export from alice's gateway, into bob's mailbox"

out="$(as alice send-session dashboard:slot-1 --to bob)"; rc=$?
assert_eq "send-session succeeds" "0" "$rc"
assert_contains "warns that the full transcript is in the bundle" "$out" "full transcript"
assert_contains "the export was asked for the slot, not the session key" \
    "$(cat "$WORK/gw-alice/exports.jsonl")" '"slot": "slot-1"'
assert_contains "Layer B was not asked for" "$(cat "$WORK/gw-alice/exports.jsonl")" '"include_layer_b": false'

FILE1="$(mailbox_files)"
assert_eq "exactly one file in the mailbox" "1" "$(printf '%s\n' "$FILE1" | grep -c .)"
assert_contains "it is addressed to bob" "$FILE1" "/remote/mailbox/bob/"
NAME1="$(basename "$FILE1")"
assert_contains "the name carries the sender" "$NAME1" "--alice--"
assert_contains "and upstream's title slug" "$NAME1" "--deploy-notes.kcsession.json.gz"
assert_eq "origin is stamped with the sender label" "alice" "$(bundle_field "$FILE1" origin)"
assert_eq "the transcript is intact" "2" "$(bundle_field "$FILE1" messages | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
assert_eq "Layer B withheld by default" "true" "$(bundle_field "$FILE1" layer_b_skipped)"
assert_eq "bundle_version untouched" "2" "$(bundle_field "$FILE1" bundle_version)"

# --------------------------------------------------------------------------
head_ "3. Mailbox traffic stays out of sync"

as alice sync > /dev/null; rc_a=$?
as bob sync > /dev/null; rc_b=$?
as alice sync > /dev/null
assert_eq "alice syncs" "0" "$rc_a"
assert_eq "bob syncs" "0" "$rc_b"
assert_eq "the mailbox file survives both publishes" "$FILE1" "$(mailbox_files)"
paths="$(published_paths)"
assert_not_contains "no mailbox path in any published bundle" "$paths" "mailbox"
assert_not_contains "no session bundle in any published bundle" "$paths" "kcsession"
repo_files="$(git -C "$WORK/bob/.sync/repo" ls-files; git -C "$WORK/alice/.sync/repo" ls-files)"
assert_not_contains "no mailbox path in either sync repo" "$repo_files" "mailbox"
assert_contains "while sessions themselves still sync (sanity)" "$paths" "files/sessions/"

# The local backend's pull fallback must not copy mailbox/ into the sync's
# scratch payload either; checked directly against the backend function.
(
    log_info() { :; }; log_success() { :; }; log_warn() { :; }; log_error() { :; }
    export LOCAL_SYNC_DIR="$WORK/remote"
    # shellcheck source=/dev/null
    source "$ROOT/backends/local.sh"
    tmp="$(mktemp -d)"
    backend_pull "$tmp"
    if [ -e "$tmp/mailbox" ]; then echo "PULLED_MAILBOX"; fi
    if [ -d "$tmp/bundles" ]; then echo "PULLED_BUNDLES"; fi
    rm -rf "$tmp"
) > "$WORK/pull-check" 2>&1
assert_not_contains "backend_pull leaves mailbox/ behind" "$(cat "$WORK/pull-check")" "PULLED_MAILBOX"
assert_contains "backend_pull still brings bundles/" "$(cat "$WORK/pull-check")" "PULLED_BUNDLES"

# --------------------------------------------------------------------------
head_ "4. inbox: list, then install through bob's gateway"

out="$(as bob inbox)"; rc=$?
assert_eq "inbox lists" "0" "$rc"
assert_contains "bob sees the bundle" "$out" "$NAME1"
assert_contains "with its sender" "$out" "from alice to you"

out="$(as alice inbox --list)"
assert_not_contains "alice does not see bob's mail" "$out" "$NAME1"

touch "$WORK/before-install"
sleep 1
out="$(as bob inbox --install)"; rc=$?
assert_eq "inbox --install succeeds" "0" "$rc"
assert_contains "reports the new session" "$out" "installed as slot-imported-"
assert_eq "bob's gateway imported exactly once" "1" "$(import_count bob)"
assert_contains "with the sender as origin (-> Imported / from alice)" "$(imports_of bob)" '"origin": "alice"'
assert_contains "and the full transcript" "$(imports_of bob)" '"messages": 2'
assert_eq "alice's gateway imported nothing" "0" "$(import_count alice)"
assert_eq "the delivered copy is removed from the backend" "" "$(mailbox_files)"
assert_contains "the ledger records it" "$(cat "$WORK/bob/.sync/mailbox-state.json")" "$NAME1"
written="$(find "$WORK/bob" -newer "$WORK/before-install" -type f ! -path "*/.sync/*")"
assert_eq "nothing was written into bob's KiroCrew data directly" "" "$written"

out="$(as bob inbox --install)"; rc=$?
assert_eq "a second --install succeeds" "0" "$rc"
assert_contains "and finds nothing new" "$out" "Nothing new to install"
assert_eq "still exactly one import" "1" "$(import_count bob)"

# --------------------------------------------------------------------------
head_ "5. The same bytes under another name are not installed twice"

# What a re-upload, or a direct send plus a broadcast, looks like: identical
# bytes, different file name. Import is copy-never-move, so a second install
# would be a second session. Reconstruct the delivered bytes from a fresh
# send, install it, then plant a copy under a new name.
as alice send-session slot-2 --to bob > /dev/null
FILE2="$(mailbox_files)"
NAME2="$(basename "$FILE2")"
cp "$FILE2" "$WORK/copy-of-2"
as bob inbox --install "$NAME2" > /dev/null
assert_eq "slot-2 installed once" "2" "$(import_count bob)"
mkdir -p "$WORK/remote/mailbox/bob"
COPY_NAME="20260922T235959Z-ffffff--alice--weekly-plan.kcsession.json.gz"
cp "$WORK/copy-of-2" "$WORK/remote/mailbox/bob/$COPY_NAME"
out="$(as bob inbox --install)"; rc=$?
assert_eq "installing the renamed copy exits 0" "0" "$rc"
assert_contains "and says it is a duplicate" "$out" "already installed"
assert_eq "no second import" "2" "$(import_count bob)"

out="$(as bob inbox --install "$NAME2")"; rc=$?
assert_eq "naming an installed bundle again is refused without --force" "1" "$rc"

# --------------------------------------------------------------------------
head_ "6. Broadcast to all of your machines (personal scope default)"

out="$(as alice send-session slot-3)"; rc=$?
assert_eq "send-session without --to in personal scope succeeds" "0" "$rc"
FILE3="$(mailbox_files | grep '/mailbox/all/')"
assert_contains "it goes to all/" "$FILE3" "/remote/mailbox/all/"
out="$(as alice inbox)"
assert_not_contains "the sender does not see its own broadcast" "$out" "broadcast-me"
out="$(as bob inbox)"
assert_contains "bob sees it, marked as sent to everyone" "$out" "from alice to everyone"
as bob inbox --install > /dev/null
assert_eq "bob installed it" "3" "$(import_count bob)"
assert_eq "the broadcast copy stays for the other machines" "$FILE3" "$(mailbox_files | grep '/mailbox/all/')"
out="$(as bob inbox)"
assert_not_contains "and is no longer listed as new for bob" "$out" "broadcast-me"
out="$(as bob inbox --all)"
assert_contains "--all shows it as installed" "$out" "[installed]"

# --------------------------------------------------------------------------
head_ "7. --discard"

as alice send-session slot-2 --to bob > /dev/null
NAME4="$(basename "$(mailbox_files | grep '/mailbox/bob/')")"
out="$(as bob inbox --discard "$NAME4")"; rc=$?
assert_eq "discard succeeds" "0" "$rc"
assert_eq "a discarded direct send is removed from the backend" "" "$(mailbox_files | grep '/mailbox/bob/' || true)"
as bob inbox --install > /dev/null
assert_eq "and never installed" "3" "$(import_count bob)"
out="$(as bob inbox --discard no-such.kcsession.json.gz)"; rc=$?
assert_eq "discarding an unknown name exits 1" "1" "$rc"

# --------------------------------------------------------------------------
head_ "8. Layer B only on request, and only if KiroCrew permits it"

out="$(as alice send-session slot-1 --to bob --include-layer-b)"
assert_contains "the export was asked for Layer B" "$(tail -1 "$WORK/gw-alice/exports.jsonl")" '"include_layer_b": true'
assert_contains "warns that Layer B is unredacted" "$out" "UNREDACTED"
assert_contains "and says KiroCrew withheld it" "$out" "export_include_layer_b"
as bob inbox --discard "$(basename "$(mailbox_files | grep '/mailbox/bob/')")" > /dev/null

# --------------------------------------------------------------------------
head_ "9. Team scope is explicit"

out="$(as alice send-session slot-2 --team)"; rc=$?
assert_eq "team scope without --to is refused" "1" "$rc"
out="$(as alice send-session slot-2 --team --to all --share-transcript)"; rc=$?
assert_eq "team scope broadcast is refused" "1" "$rc"
out="$(as alice send-session slot-2 --team --to bob)"; rc=$?
assert_eq "team scope without --share-transcript is refused" "1" "$rc"
assert_contains "and explains what would leave" "$out" "never publishes transcripts"
assert_eq "none of those uploaded anything" "" "$(mailbox_files | grep '/mailbox/bob/' || true)"
out="$(as alice send-session slot-2 --team --to bob --share-transcript)"; rc=$?
assert_eq "with --to and --share-transcript it is sent" "0" "$rc"
as bob inbox --discard "$(basename "$(mailbox_files | grep '/mailbox/bob/')")" > /dev/null

# --------------------------------------------------------------------------
head_ "10. Rejections and a stopped gateway on the receiving side"

printf 'not a gzip' > "$WORK/remote/mailbox/bob/20260922T000000Z-000000--alice--bad.kcsession.json.gz"
out="$(as bob inbox --install)"; rc=$?
assert_eq "a corrupt bundle fails the install" "1" "$rc"
assert_contains "and says why" "$out" "not a gzip file"
assert_contains "the corrupt bundle is still listed" "$(as bob inbox)" "--bad.kcsession.json.gz"
rm -f "$WORK/remote/mailbox/bob/"*--bad.kcsession.json.gz

# A slot bob has never installed: identical exports are identical bytes
# (gzip mtime=0), so re-sending slot-2 would be skipped as a duplicate
# before the gateway is ever asked.
as alice send-session slot-4 --to bob > /dev/null
write_config bob "$DEAD_PORT"
out="$(as bob inbox --install)"; rc=$?
assert_eq "install with bob's gateway down exits 1" "1" "$rc"
assert_contains "and says KiroCrew is not running" "$out" "KiroCrew is not running"
out="$(as bob inbox)"; rc=$?
assert_eq "listing still works without a gateway" "0" "$rc"
assert_contains "and the bundle is still waiting" "$out" "from alice to you"

# --------------------------------------------------------------------------
head_ "11. The gateway's unix socket is preferred when present"

# Bob's port now answers nothing over TCP; a socket at the path upstream's
# dashboard_socket_path() uses must be enough on its own.
SOCK="$WORK/bob/dashboard-$DEAD_PORT.sock"
mkdir -p "$WORK/gw-bob-unix"
python3 "$TEST_DIR/fake_gateway.py" --secret "$(cat "$WORK/bob/.local_secret")" \
    --state "$WORK/gw-bob-unix" --slots "$WORK/slots.json" --unix "$SOCK" \
    > "$WORK/gw-bob-unix/log" 2>&1 &
GATEWAY_PIDS+=($!)
for i in $(seq 1 50); do [ -S "$SOCK" ] && break; sleep 0.1; done
out="$(as bob inbox --install)"; rc=$?
assert_eq "install over the unix socket succeeds" "0" "$rc"
assert_eq "the socket gateway did the import" "1" "$(import_count bob-unix)"

# --------------------------------------------------------------------------
echo
echo "-----------------------------------------"
echo -e "  ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}"
echo "  workdir: $WORK"
echo "-----------------------------------------"
[ "$FAIL" -eq 0 ]
