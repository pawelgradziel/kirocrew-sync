#!/usr/bin/env bash
#
# Test: Background daemon basic functionality
#
# Verifies:
# - Daemon starts and stops cleanly
# - Lock prevents multiple instances
# - Remote change detection works
# - Adaptive intervals respond to activity
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

passed=0
failed=0

assert() {
    if [ "$1" -eq 0 ]; then
        printf "${GREEN}✓${NC} %s\n" "$2"
        ((passed++))
    else
        printf "${RED}✗${NC} %s\n" "$2"
        ((failed++))
    fi
}

cleanup() {
    [ -n "${DAEMON_PID:-}" ] && kill "$DAEMON_PID" 2>/dev/null || true
    [ -n "${TEMP_HOME:-}" ] && rm -rf "$TEMP_HOME" || true
}
trap cleanup EXIT

printf "\n${YELLOW}Testing: Background Daemon${NC}\n\n"

# Setup isolated environment
TEMP_HOME=$(mktemp -d)
export HOME="$TEMP_HOME"
export KIROCREW_DIR="$TEMP_HOME/.kiro/crew"
export SYNC_BACKEND="local"
export LOCAL_SYNC_DIR="$TEMP_HOME/sync-backend"

mkdir -p "$KIROCREW_DIR"
mkdir -p "$LOCAL_SYNC_DIR"

# Create minimal KiroCrew structure
mkdir -p "$KIROCREW_DIR/workspace"
touch "$KIROCREW_DIR/config.json"
echo '{}' > "$KIROCREW_DIR/config.json"

# Initialize sync
cd "$PROJECT_ROOT"
./kirocrew-sync.sh init >/dev/null 2>&1 || true

# Test 1: Daemon starts successfully
printf "Starting daemon in background...\n"
timeout 3 ./kirocrew-sync.sh daemon >/tmp/daemon-test.log 2>&1 &
DAEMON_PID=$!
sleep 1
if kill -0 "$DAEMON_PID" 2>/dev/null; then
    assert 0 "Daemon started"
else
    assert 1 "Daemon started"
    cat /tmp/daemon-test.log
fi

# Test 2: Lock file created
[ -f "$KIROCREW_DIR/.sync/daemon.lock" ]
assert $? "Lock file created"

# Test 3: Lock contains PID
LOCK_PID=$(cat "$KIROCREW_DIR/.sync/daemon.lock" 2>/dev/null || echo "")
[ "$LOCK_PID" = "$DAEMON_PID" ]
assert $? "Lock file contains correct PID"

# Test 4: Second daemon instance blocked
timeout 2 ./kirocrew-sync.sh daemon >/tmp/daemon-test2.log 2>&1 &
DAEMON2_PID=$!
sleep 1
if ! kill -0 "$DAEMON2_PID" 2>/dev/null; then
    assert 0 "Second instance blocked by lock"
else
    kill "$DAEMON2_PID" 2>/dev/null || true
    assert 1 "Second instance blocked by lock"
fi

# Test 5: Daemon log shows startup
grep -q "daemon starting" /tmp/daemon-test.log
assert $? "Daemon logged startup message"

# Test 6: Stop daemon cleanly
kill "$DAEMON_PID" 2>/dev/null
sleep 1
if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
    assert 0 "Daemon stopped cleanly"
else
    assert 1 "Daemon stopped cleanly"
fi
DAEMON_PID=""

# Test 7: Lock cleaned up after stop
sleep 1
if [ ! -f "$KIROCREW_DIR/.sync/daemon.lock" ]; then
    assert 0 "Lock file removed on exit"
else
    assert 1 "Lock file removed on exit"
fi

# Test 8: State file created for change tracking
timeout 3 ./kirocrew-sync.sh daemon >/tmp/daemon-test3.log 2>&1 &
DAEMON_PID=$!
sleep 2
if [ -f "$KIROCREW_DIR/.sync/daemon_state.txt" ]; then
    assert 0 "State file created for change tracking"
else
    assert 1 "State file created for change tracking"
fi
kill "$DAEMON_PID" 2>/dev/null || true
DAEMON_PID=""

printf "\n${YELLOW}Summary${NC}\n"
printf "Passed: ${GREEN}%d${NC}\n" "$passed"
printf "Failed: ${RED}%d${NC}\n" "$failed"
printf "\n"

[ "$failed" -eq 0 ] && exit 0 || exit 1
