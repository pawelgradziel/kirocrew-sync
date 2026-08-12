#!/usr/bin/env bash
#
# Background sync daemon - intelligent polling with adaptive intervals
#
# Use case #1: working on one machine, switching to another
#   → polls remote for changes; short sleep when changes detected
#
# Use case #2: team collaboration, changes arriving from colleagues
#   → same: pulls immediately when remote changes, backs off when idle
#
# Exit codes propagate from sync: 0 success, 1 stopped, 3 partial (quarantine)
#

set -euo pipefail

# Must be sourced by kirocrew-sync.sh, not run standalone
if [ -z "${SCRIPT_DIR:-}" ]; then
    echo "Error: daemon.sh must be sourced by kirocrew-sync.sh" >&2
    exit 1
fi

# Adaptive intervals -- the standalone-daemon baseline, used whenever the
# KiroCrew app isn't installed or its configured interval can't be read
# (see refresh_effective_intervals() and EFF_INTERVAL_* below, which are
# what daemon_cycle actually uses; these constants are their fallback):
# - idle: 5 minutes when nothing is happening
# - active: 30 seconds after a successful sync with changes
# - backoff: 10 minutes after sync failure or conflict
INTERVAL_IDLE=300
INTERVAL_ACTIVE=30
INTERVAL_BACKOFF=600

# Track last remote state to detect changes without a full sync
DAEMON_STATE="$SYNC_ROOT/daemon_state.txt"
DAEMON_LOCK="$SYNC_ROOT/daemon.lock"

# --- App integration -------------------------------------------------------
#
# When the KiroCrew app (app/backend) is installed, a daemon tick should
# produce a recorded run -- a sync_runs row, conflict/quarantine ingestion,
# notifications -- exactly like a "Sync Now" click, rather than running the
# engine directly and leaving no trace the app can show. app/backend/cli.py
# is the app's own dedicated entry point for exactly this (see its module
# docstring); it shells out to this same kirocrew-sync.sh and then records
# what happened via app/backend/sync_runner.py's run_sync_and_record(). This
# section detects that entry point and, when present, delegates to it
# instead of re-implementing any of that recording in bash.
#
# The layout below mirrors app.json's own cron command and
# sync_runner.resolve_db_path() on purpose -- two independent guesses at
# where the app lives would be a standing invitation to drift apart.
APP_DIR="$KIROCREW_DIR/apps/kirocrew-sync"
APP_CLI="$APP_DIR/backend/cli.py"
APP_PYTHON="$APP_DIR/.venv/bin/python3"

# True iff the app's standalone entry point is present and runnable. This is
# the *only* signal used to decide whether to delegate a tick to cli.py --
# it does not depend on sqlite3 or the database existing (see
# read_app_interval() below for that, a separate and independently-optional
# capability).
app_installed() {
    [ -f "$APP_CLI" ] && [ -x "$APP_PYTHON" ]
}

# Mirrors app/backend/sync_runner.py's resolve_db_path(): an explicit
# KIROCREW_SYNC_DB wins, otherwise <KIROCREW_DIR>/apps/kirocrew-sync/data/
# history.db. Deliberately not a third scheme -- read-only reuse of the
# app's own resolution.
app_db_path() {
    if [ -n "${KIROCREW_SYNC_DB:-}" ]; then
        printf '%s\n' "$KIROCREW_SYNC_DB"
    else
        printf '%s\n' "$APP_DIR/data/history.db"
    fi
}

# Cached sqlite3-availability check (looked up once per process, not once
# per cycle). The `sqlite3` CLI is not guaranteed to be installed --
# reading daemon_state is a nice-to-have, not a requirement for delegating
# to cli.py, which never needs it (cli.py talks to the database through
# Python's stdlib sqlite3 module, in its own process).
_HAVE_SQLITE3=""
have_sqlite3() {
    if [ -z "$_HAVE_SQLITE3" ]; then
        if command -v sqlite3 >/dev/null 2>&1; then
            _HAVE_SQLITE3=1
        else
            _HAVE_SQLITE3=0
        fi
    fi
    [ "$_HAVE_SQLITE3" = "1" ]
}

# Echoes the app-configured polling interval (seconds, 60-900) from the
# daemon_state table, or returns 1 with nothing echoed if it cannot be
# determined -- app not installed, sqlite3 missing, database not created
# yet, table/row missing, or a non-numeric value. Callers must treat
# failure as "config unavailable" and fall back to the hardcoded defaults,
# not as "0".
read_app_interval() {
    app_installed || return 1
    have_sqlite3 || return 1

    local db
    db="$(app_db_path)"
    [ -f "$db" ] || return 1

    local value
    # busy_timeout so a concurrent writer (the app's server, or another
    # cli.py tick) makes this wait briefly rather than fail outright; still
    # bounded so a stuck writer can't hang the daemon's polling loop.
    value=$(sqlite3 -cmd "PRAGMA busy_timeout=1000" "$db" \
        "SELECT value FROM daemon_state WHERE key = 'interval';" 2>/dev/null) || return 1

    case "$value" in
        ''|*[!0-9]*) return 1 ;;
    esac

    # update_daemon_config() enforces 60-900 on write; re-clamp defensively
    # in case the row was ever hand-edited.
    [ "$value" -lt 60 ] && value=60
    [ "$value" -gt 900 ] && value=900
    printf '%s\n' "$value"
}

# Effective intervals actually used by daemon_cycle -- start out equal to
# the hardcoded defaults and get refreshed once per cycle by
# refresh_effective_intervals(). Kept as separate variables (rather than
# overwriting INTERVAL_IDLE/ACTIVE/BACKOFF) so those constants keep
# documenting the no-app-installed baseline this daemon has always used.
EFF_INTERVAL_IDLE=$INTERVAL_IDLE
EFF_INTERVAL_ACTIVE=$INTERVAL_ACTIVE
EFF_INTERVAL_BACKOFF=$INTERVAL_BACKOFF

# Reconciles the app's single user-configured "how often" number with this
# daemon's three-state adaptive polling. The dashboard slider is one value
# (60-900s); this daemon has three (idle/active/backoff). Flattening to one
# number everywhere would throw away the adaptive behavior entirely (no
# more "check back in 30s after something happened", no more "back off for
# longer after a failure") -- so instead the configured value becomes the
# new *idle* baseline (that is what "how often should this poll" means when
# nothing is happening), and active/backoff are rescaled around it, keeping
# the same ratios as the hardcoded defaults (active = idle/10, backoff =
# idle*2 -- i.e. 300/30/600 today). A user who asks for a slower or faster
# baseline still gets proportionally faster follow-up checks and
# proportionally longer backoff, not a flat interval.
#
# Falls back to the hardcoded INTERVAL_* constants, untouched, whenever
# read_app_interval() can't produce a value -- no app installed, no
# sqlite3, database not created yet, etc. Refreshed once per cycle (not
# just once at startup) so a change made on the dashboard while this daemon
# is already running takes effect on the next tick rather than requiring a
# restart.
refresh_effective_intervals() {
    local configured
    if configured=$(read_app_interval); then
        EFF_INTERVAL_IDLE=$configured
        EFF_INTERVAL_ACTIVE=$(( configured / 10 ))
        [ "$EFF_INTERVAL_ACTIVE" -lt 10 ] && EFF_INTERVAL_ACTIVE=10
        EFF_INTERVAL_BACKOFF=$(( configured * 2 ))
    else
        EFF_INTERVAL_IDLE=$INTERVAL_IDLE
        EFF_INTERVAL_ACTIVE=$INTERVAL_ACTIVE
        EFF_INTERVAL_BACKOFF=$INTERVAL_BACKOFF
    fi
}

# Standalone path: run the engine directly, exactly as this daemon always
# has. Used when the app is not installed.
run_sync_via_script() {
    local rc=0
    "$SCRIPT_DIR/kirocrew-sync.sh" sync --strategy auto 2>&1 | grep -E "^(✗|⚠|✓.*conflict|✓.*quarantine)" || rc=$?
    return "$rc"
}

# App-delegated path: run the same sync through the app's cli.py so this
# tick gets recorded (sync_runs row, conflict/quarantine ingestion,
# notifications) exactly like a "Sync Now" click or the app's own cron tick
# -- see app/backend/cli.py and app/backend/sync_runner.py (read-only,
# owned elsewhere). Used when app_installed() is true.
run_sync_via_app() {
    # ${team_args[@]+"${team_args[@]}"} rather than "${team_args[@]}": bash
    # 3.2, still the system bash on macOS, treats an empty array as unset
    # under `set -u` (see kirocrew-sync.sh's merge_remote_refs() for the
    # same pattern).
    local team_args=()
    [ "$SYNC_SCOPE" = "team" ] && team_args=(--team)

    local output
    local rc=0
    output=$("$APP_PYTHON" "$APP_CLI" --strategy auto ${team_args[@]+"${team_args[@]}"} 2>&1) || rc=$?

    # Keep daemon logs about as quiet in this mode as in the standalone
    # one: surface only cli.py's own one-line summary/failure messages
    # (see its logger.info/error calls), not its full INFO-level chatter.
    printf '%s\n' "$output" | grep -E "cron sync (finished|failed|could not|skipped|timed out)" || true

    # Recover the underlying engine exit code from cli.py's own summary log
    # line ("cron sync finished: run=... engine_exit=N ...") when present,
    # so quarantine (engine exit 3, which cli.py deliberately maps to its
    # own process exit 0 -- see cli.py's EXIT CODES docstring) still gets
    # the same "back to idle cadence, not a fast recheck" treatment the
    # standalone path gives it below, instead of being indistinguishable
    # from a completely clean run. Falls back to cli.py's own exit code --
    # a coarser but still correct ok/failed signal -- if that line is ever
    # missing (e.g. the already-running short-circuit, which logs a
    # different message and never reaches it) or its shape changes.
    local engine_exit
    # `|| engine_exit=""` makes this self-contained under `set -e`
    # regardless of how this function is called: grep finding no match
    # (the common case when the "engine_exit=" line isn't present at all)
    # makes the pipeline fail under `pipefail`, which would otherwise abort
    # the whole daemon right here. Today's only caller already wraps this
    # function in `|| rc=$?` (daemon_cycle), which happens to make -e
    # ignore failures anywhere inside it too -- but relying on that from
    # inside here would be a silent trap for the next caller that doesn't.
    engine_exit=$(printf '%s\n' "$output" | grep -oE 'engine_exit=-?[0-9]+' | tail -1 | cut -d= -f2) || engine_exit=""
    [ -z "$engine_exit" ] && engine_exit=$rc
    return "$engine_exit"
}

daemon_log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

acquire_daemon_lock() {
    # Prevent multiple daemon instances
    local lock_age=0
    if [ -f "$DAEMON_LOCK" ]; then
        local lock_pid
        lock_pid=$(cat "$DAEMON_LOCK" 2>/dev/null || echo "")
        if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
            daemon_log "Another daemon is running (PID $lock_pid)"
            return 1
        fi
        # Stale lock - clean it
        daemon_log "Cleaning stale lock (PID $lock_pid not running)"
        rm -f "$DAEMON_LOCK"
    fi
    echo $$ > "$DAEMON_LOCK"
    return 0
}

release_daemon_lock() {
    rm -f "$DAEMON_LOCK"
}

# Check if remote has changes without doing a full sync
check_remote_changed() {
    local current_state
    current_state=$(backend_list 2>/dev/null || echo "")
    
    if [ -z "$current_state" ]; then
        daemon_log "Cannot reach backend (offline or auth issue)"
        return 2  # indeterminate
    fi
    
    local last_state=""
    if [ -f "$DAEMON_STATE" ]; then
        last_state=$(cat "$DAEMON_STATE")
    fi
    
    if [ "$current_state" != "$last_state" ]; then
        echo "$current_state" > "$DAEMON_STATE"
        return 0  # changed
    fi
    
    return 1  # unchanged
}

# Smart sync: run only if KiroCrew is closed, or if it's open but idle
should_sync_now() {
    if [ "${KIROCREW_SYNC_SKIP_RUNNING_CHECK:-0}" = "1" ]; then
        return 0
    fi

    local self
    self="$(basename "${BASH_SOURCE[0]}")"
    local kirocrew_pids
    kirocrew_pids=$(pgrep -f "kirocrew" 2>/dev/null | grep -v "^$$\$" | grep -v "$self" || true)
    
    if [ -z "$kirocrew_pids" ]; then
        # KiroCrew not running - safe to sync
        return 0
    fi
    
    # KiroCrew is running - check if databases are being written
    # (heuristic: no writes to .db files in last 60 seconds)
    # Exclude our own writes, or this never reports idle: $SYNC_ROOT holds the
    # sync repo and this script's own pre-pack backups, and $KIROCREW_DIR/apps
    # holds app-private databases -- including the sync app's history.db, which
    # gains a row on every single run. Mirrors check_kirocrew_running() in
    # kirocrew-sync.sh; keep the two in step.
    local recent_writes
    recent_writes=$(find "$KIROCREW_DIR" \
        -path "$SYNC_ROOT/*" -prune -o \
        -path "$KIROCREW_DIR/apps/*" -prune -o \
        -name "*.db" -mmin -1 -print 2>/dev/null | wc -l)
    
    if [ "$recent_writes" -eq 0 ]; then
        daemon_log "KiroCrew running but idle, syncing..."
        return 0
    fi
    
    daemon_log "KiroCrew active, skipping sync"
    return 1
}

# Set by daemon_cycle on every exit path; cmd_daemon reads it after the call
# to decide how long to sleep. Deliberately NOT communicated via the
# function's own exit status the way this used to work: bash truncates exit
# statuses to 0-255, and this daemon's real interval range is 60-1800s
# (EFF_INTERVAL_BACKOFF can reach 1800 -- an app-configured interval of 900,
# doubled). Returning that as an exit code silently wraps: 1800 mod 256 is
# 8, so a user who configured a 900s interval would see backoff sleeps of
# 8 seconds, not 1800. Verified this isn't hypothetical: even the original
# hardcoded INTERVAL_BACKOFF=600 already wrapped this way on unmodified
# master (600 mod 256 == 88) -- `./kirocrew-sync.sh daemon` genuinely logs
# "Next check in 88s", not 600s, after a single unreachable-backend cycle.
# A plain variable has no such limit.
DAEMON_NEXT_INTERVAL=$INTERVAL_IDLE

daemon_cycle() {
    # Pick up any dashboard-configured interval change before this cycle
    # decides anything time-related. A no-op (falls straight back to the
    # hardcoded defaults) when the app isn't installed or sqlite3 isn't
    # available -- see refresh_effective_intervals()'s docstring.
    refresh_effective_intervals

    DAEMON_NEXT_INTERVAL=$EFF_INTERVAL_IDLE
    local changes_detected=0

    # Check if remote changed first (fast, no merge)
    if check_remote_changed; then
        changes_detected=1
        daemon_log "Remote changes detected"
    else
        local rc=$?
        if [ $rc -eq 2 ]; then
            daemon_log "Backend unreachable, will retry"
            DAEMON_NEXT_INTERVAL=$EFF_INTERVAL_BACKOFF
            return 0
        fi
    fi

    # Also check if local changed (compare last git commit time)
    if [ -d "$SYNC_REPO/.git" ]; then
        local last_commit_time
        last_commit_time=$(git -C "$SYNC_REPO" log -1 --format=%ct 2>/dev/null || echo "0")
        local last_pack_time
        # Cross-platform stat: -c %Y on Linux, -f %m on macOS
        if [[ "$OSTYPE" == "darwin"* ]]; then
            last_pack_time=$(stat -f %m "$SYNC_REPO/.git/refs/heads/$SYNC_BRANCH" 2>/dev/null || echo "0")
        else
            last_pack_time=$(stat -c %Y "$SYNC_REPO/.git/refs/heads/$SYNC_BRANCH" 2>/dev/null || echo "0")
        fi
        local now
        now=$(date +%s)

        # If last pack was within the idle interval, local changes may exist
        if [ $((now - last_pack_time)) -lt $EFF_INTERVAL_IDLE ]; then
            changes_detected=1
            daemon_log "Local changes detected (recent commit)"
        fi
    fi

    if [ $changes_detected -eq 0 ]; then
        daemon_log "No changes detected, sleeping..."
        DAEMON_NEXT_INTERVAL=$EFF_INTERVAL_IDLE
        return 0
    fi

    if ! should_sync_now; then
        daemon_log "Deferring sync (KiroCrew busy)"
        DAEMON_NEXT_INTERVAL=$EFF_INTERVAL_ACTIVE  # check again soon
        return 0
    fi

    daemon_log "Starting sync..."

    # Delegate to the app's cli.py when it's installed (so this tick gets
    # recorded), otherwise run the engine directly exactly as before.
    local rc=0
    if app_installed; then
        run_sync_via_app || rc=$?
    else
        run_sync_via_script || rc=$?
    fi

    case $rc in
        0)
            daemon_log "Sync completed successfully"
            DAEMON_NEXT_INTERVAL=$EFF_INTERVAL_ACTIVE  # changes happened, check again soon
            ;;
        3)
            daemon_log "Sync completed with quarantine"
            DAEMON_NEXT_INTERVAL=$EFF_INTERVAL_IDLE  # partial success
            ;;
        *)
            daemon_log "Sync failed (exit $rc)"
            DAEMON_NEXT_INTERVAL=$EFF_INTERVAL_BACKOFF  # back off on failure
            ;;
    esac

    return 0
}

cmd_daemon() {
    daemon_log "KiroCrew Sync daemon starting (scope: $SYNC_SCOPE)"
    if app_installed; then
        daemon_log "KiroCrew app detected at $APP_DIR: sync ticks will be delegated to its cli.py for recording"
        if have_sqlite3; then
            daemon_log "sqlite3 available: will honor the app's configured polling interval"
        else
            daemon_log "sqlite3 not found: using built-in adaptive intervals (cannot read the app's configured interval)"
        fi
    fi

    if ! acquire_daemon_lock; then
        exit 1
    fi
    
    # INT/TERM need their own handler that actually exits. A bare
    # `trap release_daemon_lock INT TERM` runs the handler and then *resumes*
    # the polling loop, so the daemon survives the signal having already
    # deleted its own lock -- at which point a second daemon acquires the lock
    # and both sync concurrently. The EXIT trap still covers ordinary exits;
    # release_daemon_lock is `rm -f`, so running it twice is harmless.
    trap release_daemon_lock EXIT
    trap 'release_daemon_lock; exit 0' INT TERM
    
    # Initial state capture
    check_remote_changed || true
    
    while true; do
        daemon_cycle
        daemon_log "Next check in ${DAEMON_NEXT_INTERVAL}s"
        sleep "$DAEMON_NEXT_INTERVAL"
    done
}
