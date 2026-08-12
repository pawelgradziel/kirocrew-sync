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

# Adaptive intervals:
# - idle: 5 minutes when nothing is happening
# - active: 30 seconds after a successful sync with changes
# - backoff: 10 minutes after sync failure or conflict
INTERVAL_IDLE=300
INTERVAL_ACTIVE=30
INTERVAL_BACKOFF=600

# Track last remote state to detect changes without a full sync
DAEMON_STATE="$SYNC_ROOT/daemon_state.txt"
DAEMON_LOCK="$SYNC_ROOT/daemon.lock"

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
    local recent_writes
    recent_writes=$(find "$KIROCREW_DIR" -name "*.db" -mmin -1 2>/dev/null | wc -l)
    
    if [ "$recent_writes" -eq 0 ]; then
        daemon_log "KiroCrew running but idle, syncing..."
        return 0
    fi
    
    daemon_log "KiroCrew active, skipping sync"
    return 1
}

daemon_cycle() {
    local interval=$INTERVAL_IDLE
    local changes_detected=0
    
    # Check if remote changed first (fast, no merge)
    if check_remote_changed; then
        changes_detected=1
        daemon_log "Remote changes detected"
    else
        local rc=$?
        if [ $rc -eq 2 ]; then
            daemon_log "Backend unreachable, will retry"
            return $INTERVAL_BACKOFF
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
        if [ $((now - last_pack_time)) -lt $INTERVAL_IDLE ]; then
            changes_detected=1
            daemon_log "Local changes detected (recent commit)"
        fi
    fi
    
    if [ $changes_detected -eq 0 ]; then
        daemon_log "No changes detected, sleeping..."
        return $INTERVAL_IDLE
    fi
    
    if ! should_sync_now; then
        daemon_log "Deferring sync (KiroCrew busy)"
        return $INTERVAL_ACTIVE  # check again soon
    fi
    
    daemon_log "Starting sync..."
    
    # Run sync in quiet mode (suppress routine output, keep errors)
    local rc=0
    if "$SCRIPT_DIR/kirocrew-sync.sh" sync --strategy auto 2>&1 | grep -E "^(✗|⚠|✓.*conflict|✓.*quarantine)" || rc=$?; then
        case $rc in
            0)
                daemon_log "Sync completed successfully"
                interval=$INTERVAL_ACTIVE  # changes happened, check again soon
                ;;
            3)
                daemon_log "Sync completed with quarantine"
                interval=$INTERVAL_IDLE  # partial success
                ;;
            *)
                daemon_log "Sync failed (exit $rc)"
                interval=$INTERVAL_BACKOFF  # back off on failure
                ;;
        esac
    fi
    
    return $interval
}

cmd_daemon() {
    daemon_log "KiroCrew Sync daemon starting (scope: $SYNC_SCOPE)"
    
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
        local next_interval=$INTERVAL_IDLE
        daemon_cycle && next_interval=$? || next_interval=$?
        
        daemon_log "Next check in ${next_interval}s"
        sleep "$next_interval"
    done
}
