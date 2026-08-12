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
    # `passed=$((passed + 1))` rather than `((passed++))`: under `set -e`,
    # `((expr))` returns the shell command's exit status based on whether
    # the *arithmetic result* is zero, not whether the command "succeeded"
    # -- so `((passed++))` (a post-increment, which evaluates to the OLD
    # value) returns exit status 1 the very first time it runs, while
    # passed is still 0, and set -e kills the whole script right there.
    # Verified: every run stopped dead after the first "Daemon started"
    # PASS, before any of the assertions that actually exercise daemon
    # behavior. An assignment's exit status is always 0 regardless of the
    # value assigned, so this form is safe under -e no matter the count.
    if [ "$1" -eq 0 ]; then
        printf "${GREEN}✓${NC} %s\n" "$2"
        passed=$((passed + 1))
    else
        printf "${RED}✗${NC} %s\n" "$2"
        failed=$((failed + 1))
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
#
# `CMD; assert $? ...` is only safe under `set -e` when CMD can't fail --
# otherwise -e kills the whole script right there, before assert() ever
# runs, and the failure never gets recorded as a red X. `CMD || rc=$?` is
# the safe idiom: a command's failure inside a `||` list is exempt from -e
# as long as it isn't the list's last command (see bash's manual, "The
# shell does not exit ... part of any command executed in a && or || list
# except the command following the final && or ||"). Same fix applied to
# Test 3 and Test 5 below.
rc=0
[ -f "$KIROCREW_DIR/.sync/daemon.lock" ] || rc=$?
assert "$rc" "Lock file created"

# Test 3: Lock contains a real PID for the daemon process this test
# actually launched.
#
# DAEMON_PID above is `timeout`'s own PID, not kirocrew-sync.sh's: unlike
# Popen-launched daemons (see sync_manager.py's start_daemon(), which execs
# the script directly so a shebang re-exec preserves the PID), `timeout N
# CMD &` forks a real monitor process that stays alive and does not
# exec-replace itself, so `$!` here and `$$` inside the daemon (what
# lib/daemon.sh's acquire_daemon_lock() writes to daemon.lock) are two
# different PIDs by construction -- verified directly with `ps`. A plain
# `[ "$LOCK_PID" = "$DAEMON_PID" ]` therefore always failed here, on
# unmodified master too, regardless of anything lib/daemon.sh does. What's
# actually true and worth asserting: the lock names a live process that is
# a *child* of the timeout wrapper this test launched.
LOCK_PID=$(cat "$KIROCREW_DIR/.sync/daemon.lock" 2>/dev/null || echo "")
rc=1
if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
    LOCK_PPID=$(ps -o ppid= -p "$LOCK_PID" 2>/dev/null | tr -d ' ')
    [ "$LOCK_PPID" = "$DAEMON_PID" ] && rc=0
fi
assert "$rc" "Lock file contains correct PID"

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
rc=0
grep -q "daemon starting" /tmp/daemon-test.log || rc=$?
assert "$rc" "Daemon logged startup message"

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
#
# Was a KNOWN PRE-EXISTING FAILURE: this asserts that check_remote_changed()
# (lib/daemon.sh) successfully writes $DAEMON_STATE, which only happens
# when its call to `backend_list` succeeds. `backend_list` used to be
# called at lib/daemon.sh:~240 but was never defined anywhere -- not in
# backends/local.sh (the backend this test suite uses), nor
# s3.sh/gdrive.sh/rsync.sh, nor anywhere in this repository's git history
# before now. Every invocation hit bash's "command not found", which the
# `2>/dev/null || echo ""` around it silently swallowed, so
# check_remote_changed() always returned 2 ("cannot reach backend") --
# forever, for every backend, since the daemon's original commit
# (d490ef4). The consequence was larger than this one assertion: every
# idle daemon_cycle took the "Backend unreachable, will retry" branch
# instead of the intended "no changes, sleep at the idle interval" one, so
# the adaptive polling this daemon advertises never actually reached its
# idle state.
#
# Fixed by adding a real backend_list() to each of local.sh/gdrive.sh/
# s3.sh/rsync.sh (see backends/local.sh for the fingerprint contract they
# all share, and tests/test_backend_local_list.sh for a dedicated test of
# it -- only local.sh is testable here without real remote credentials).
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

# --- App integration: detection, interval reading, sync dispatch ----------
#
# lib/daemon.sh delegates to the KiroCrew app's cli.py when installed, and
# reads its configured polling interval from its sqlite database when
# possible. These exercise that directly, by sourcing lib/daemon.sh into an
# isolated subshell against a fake app tree -- not by running a full daemon
# cycle end-to-end. That remains the simplest way to cover
# run_sync_via_app()/read_app_interval()/refresh_effective_intervals() in
# isolation, independent of whatever check_remote_changed() and
# should_sync_now() decide on a given tick, without needing a live
# KiroCrew app process or a real sqlite-backed daemon_state table wired
# into a running daemon.

printf "\n${YELLOW}Testing: App integration${NC}\n\n"

APP_TEST_HOME=$(mktemp -d)
APP_TEST_KIROCREW_DIR="$APP_TEST_HOME/.kiro/crew"
APP_TEST_SYNC_ROOT="$APP_TEST_KIROCREW_DIR/.sync"
mkdir -p "$APP_TEST_SYNC_ROOT"
DAEMON_TEST_SCOPE="personal"

# Sources lib/daemon.sh into an isolated subshell -- its own SCRIPT_DIR
# (PROJECT_ROOT, as kirocrew-sync.sh sets it, not this test file's own
# tests/ SCRIPT_DIR), SYNC_ROOT, KIROCREW_DIR, and SYNC_SCOPE -- and
# evaluates the snippet given as $1 in that context. Never touches this
# test's own daemon/lock state (Tests 1-8 above), nor the real ~/.kiro/crew.
in_daemon_lib() {
    (
        SCRIPT_DIR="$PROJECT_ROOT"
        SYNC_ROOT="$APP_TEST_SYNC_ROOT"
        KIROCREW_DIR="$APP_TEST_KIROCREW_DIR"
        SYNC_SCOPE="$DAEMON_TEST_SCOPE"
        SYNC_REPO="$SYNC_ROOT/repo"
        SYNC_BRANCH="main"
        # shellcheck source=lib/daemon.sh
        source "$PROJECT_ROOT/lib/daemon.sh"
        eval "$1"
    )
}

# Test 9: app not installed -> app_installed() is false.
if in_daemon_lib 'app_installed'; then
    assert 1 "app_installed() false when the app tree is absent"
else
    assert 0 "app_installed() false when the app tree is absent"
fi

# Test 10: app_installed() true once cli.py and an executable venv python
# exist at the expected layout ($KIROCREW_DIR/apps/kirocrew-sync/...,
# mirroring app.json's own cron command and sync_runner.resolve_db_path()).
APP_DIR="$APP_TEST_KIROCREW_DIR/apps/kirocrew-sync"
mkdir -p "$APP_DIR/backend" "$APP_DIR/.venv/bin"
touch "$APP_DIR/backend/cli.py"
# Fake cli.py stand-in: records its argv to a file this test can inspect
# (run_sync_via_app() only re-surfaces a filtered subset of real output, so
# a marker file is the only way to see the exact command line from outside),
# prints a canned summary line in the same shape as the real cli.py's
# logger.info() call in main(), and exits with $FAKE_CLI_EXIT (default 0).
# This lets the dispatch/parsing logic in run_sync_via_app() be exercised
# without a real Python interpreter, venv, or sqlite database.
cat > "$APP_DIR/.venv/bin/python3" <<EOF
#!/usr/bin/env bash
echo "\$*" > "$APP_TEST_HOME/last_invoke_args.txt"
echo "cron sync finished: run=1 engine_exit=\${FAKE_ENGINE_EXIT:-0} rows_merged=0 conflicts=0 quarantined=0 -> \${FAKE_CLI_EXIT:-0}"
exit "\${FAKE_CLI_EXIT:-0}"
EOF
chmod +x "$APP_DIR/.venv/bin/python3"

if in_daemon_lib 'app_installed'; then
    assert 0 "app_installed() true once cli.py and venv python exist"
else
    assert 1 "app_installed() true once cli.py and venv python exist"
fi

# Test 11: read_app_interval() is unavailable (fails) without sqlite3
# and/or a database file present, even though the app itself is installed
# -- these are independent capabilities (see read_app_interval()'s
# docstring in lib/daemon.sh).
if in_daemon_lib 'read_app_interval' >/dev/null 2>&1; then
    assert 1 "read_app_interval() unavailable without sqlite3/a database"
else
    assert 0 "read_app_interval() unavailable without sqlite3/a database"
fi

# Test 12: refresh_effective_intervals() falls back to the hardcoded
# defaults (300/30/600) when no app-configured interval is available.
result=$(in_daemon_lib 'refresh_effective_intervals; echo "$EFF_INTERVAL_IDLE $EFF_INTERVAL_ACTIVE $EFF_INTERVAL_BACKOFF"')
rc=0
[ "$result" = "300 30 600" ] || rc=1
assert "$rc" "refresh_effective_intervals() falls back to 300/30/600 without app config"

# Test 13: run_sync_via_app() dispatch -- constructs the right command line
# and recovers the engine exit code from cli.py's summary log line. The
# FAKE_ENGINE_EXIT/FAKE_CLI_EXIT exports happen *inside* the eval snippet
# (not as a shell-assignment prefix on the outer `RESULT=$(...)` line):
# `A=1 B=2 X=$(cmd)` with no actual command word is just three ordinary
# assignments in bash, not "cmd run with A/B exported" -- doing it inside
# the snippet is what actually gets these into the subshell that execs the
# fake python script.
DAEMON_TEST_SCOPE="personal"
RESULT=$(in_daemon_lib 'export FAKE_ENGINE_EXIT=3 FAKE_CLI_EXIT=0; RC=0; run_sync_via_app || RC=$?; echo "RC=$RC"')
rc=0
printf '%s\n' "$RESULT" | grep -qx "RC=3" || rc=1
assert "$rc" "run_sync_via_app() recovers engine_exit=3 (quarantine) even though cli.py itself exits 0"

rc=0
[ -f "$APP_TEST_HOME/last_invoke_args.txt" ] || rc=1
INVOKE_ARGS=$(cat "$APP_TEST_HOME/last_invoke_args.txt" 2>/dev/null || echo "")
# $* seen by the fake script is the full argv it was invoked with, i.e.
# "<path-to-cli.py> --strategy auto [--team]" ($APP_CLI is argv[0] from the
# fake python's own point of view) -- so this checks the tail of the
# command line, not an exact match against the whole thing.
case "$INVOKE_ARGS" in
    *"--strategy auto") ;;
    *) rc=1 ;;
esac
assert "$rc" "run_sync_via_app() omits --team for personal scope"

# Test 14: run_sync_via_app() adds --team when SYNC_SCOPE=team.
DAEMON_TEST_SCOPE="team"
RESULT=$(in_daemon_lib 'export FAKE_ENGINE_EXIT=0 FAKE_CLI_EXIT=0; RC=0; run_sync_via_app || RC=$?; echo "RC=$RC"')
INVOKE_ARGS=$(cat "$APP_TEST_HOME/last_invoke_args.txt" 2>/dev/null || echo "")
rc=0
case "$INVOKE_ARGS" in
    *"--strategy auto --team") ;;
    *) rc=1 ;;
esac
assert "$rc" "run_sync_via_app() adds --team for team scope"
DAEMON_TEST_SCOPE="personal"

# Test 15: run_sync_via_app() falls back to cli.py's own exit code when its
# summary line never appears (e.g. the already-running short-circuit, which
# logs a different message -- see cli.py's main()).
cat > "$APP_DIR/.venv/bin/python3" <<EOF
#!/usr/bin/env bash
echo "\$*" > "$APP_TEST_HOME/last_invoke_args.txt"
echo "cron sync skipped: a sync is already in progress"
exit 0
EOF
chmod +x "$APP_DIR/.venv/bin/python3"
RESULT=$(in_daemon_lib 'RC=0; run_sync_via_app || RC=$?; echo "RC=$RC"')
rc=0
printf '%s\n' "$RESULT" | grep -qx "RC=0" || rc=1
assert "$rc" "run_sync_via_app() falls back to cli.py's own exit code (0) when no engine_exit= line is present"

# Test 16 (requires sqlite3; skipped if not installed -- see
# have_sqlite3()'s docstring): read_app_interval()/refresh_effective_intervals()
# against a real daemon_state table, including clamping and the specific
# 900s-interval-should-not-truncate-to-8s case (see DAEMON_NEXT_INTERVAL's
# docstring in lib/daemon.sh for the exit-status-truncation bug this
# guards against).
if command -v sqlite3 >/dev/null 2>&1; then
    APP_DB="$APP_DIR/data/history.db"
    mkdir -p "$(dirname "$APP_DB")"
    sqlite3 "$APP_DB" \
        "CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP); INSERT INTO daemon_state (key, value) VALUES ('interval', '600');"

    result=$(in_daemon_lib 'read_app_interval')
    rc=0
    [ "$result" = "600" ] || rc=1
    assert "$rc" "read_app_interval() reads the app-configured interval via sqlite3"

    result=$(in_daemon_lib 'refresh_effective_intervals; echo "$EFF_INTERVAL_IDLE $EFF_INTERVAL_ACTIVE $EFF_INTERVAL_BACKOFF"')
    rc=0
    [ "$result" = "600 60 1200" ] || rc=1
    assert "$rc" "refresh_effective_intervals() rescales active/backoff (600 -> 60/1200) around the configured interval"

    sqlite3 "$APP_DB" "UPDATE daemon_state SET value = '5000' WHERE key = 'interval';"
    result=$(in_daemon_lib 'read_app_interval')
    rc=0
    [ "$result" = "900" ] || rc=1
    assert "$rc" "read_app_interval() clamps an out-of-range value to the 60-900 ceiling"

    sqlite3 "$APP_DB" "UPDATE daemon_state SET value = '900' WHERE key = 'interval';"
    result=$(in_daemon_lib 'refresh_effective_intervals; echo "$EFF_INTERVAL_BACKOFF"')
    rc=0
    [ "$result" = "1800" ] || rc=1
    assert "$rc" "a 900s configured interval backs off to 1800s, not truncated to 8 by an exit-status return"
else
    printf "${YELLOW}(skipped: sqlite3 not installed -- read_app_interval() sqlite3-backed tests)${NC}\n"
fi

rm -rf "$APP_TEST_HOME"

printf "\n${YELLOW}Summary${NC}\n"
printf "Passed: ${GREEN}%d${NC}\n" "$passed"
printf "Failed: ${RED}%d${NC}\n" "$failed"
printf "\n"

[ "$failed" -eq 0 ] && exit 0 || exit 1
