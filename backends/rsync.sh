#!/usr/bin/env bash
#
# Rsync backend for KiroCrew Sync
# Direct sync to remote host or NAS
#

# Configuration
RSYNC_HOST="${RSYNC_HOST:-user@remote-host}"
RSYNC_PATH="${RSYNC_PATH:-/path/to/kirocrew-sync}"

check_rsync() {
    if ! command -v rsync &> /dev/null; then
        log_error "rsync is not installed"
        log_info "Install rsync:"
        log_info "  macOS:   brew install rsync"
        log_info "  Linux:   sudo apt install rsync  (or yum/dnf)"
        exit 1
    fi
}

check_rsync_configured() {
    if ! ssh "${RSYNC_HOST%%:*}" "test -d $RSYNC_PATH" 2>/dev/null; then
        log_error "Cannot access remote host: ${RSYNC_HOST}"
        log_info ""
        log_info "To configure rsync backend:"
        log_info ""
        log_info "1. Set up SSH key authentication:"
        log_info "   ssh-copy-id $RSYNC_HOST"
        log_info ""
        log_info "2. Create remote directory:"
        log_info "   ssh $RSYNC_HOST 'mkdir -p $RSYNC_PATH'"
        log_info ""
        log_info "3. Update backends/rsync.sh with:"
        log_info "   - RSYNC_HOST: your SSH user@host"
        log_info "   - RSYNC_PATH: remote directory path"
        log_info ""
        exit 1
    fi
}

backend_push() {
    local bundle_dir="$1"
    
    check_rsync
    check_rsync_configured
    
    log_info "Syncing to remote host..."
    
    # mailbox/ holds send-session bundles, not sync state; --delete must
    # never reach it (an excluded path is also protected from deletion).
    if rsync -avz --delete \
        --exclude="*.lock" \
        --exclude="*.tmp" \
        --exclude="/mailbox/" \
        "$bundle_dir/" "${RSYNC_HOST}:${RSYNC_PATH}/"; then
        log_success "Synced to ${RSYNC_HOST}:${RSYNC_PATH}/"
    else
        log_error "Sync failed"
        return 1
    fi
}

backend_pull() {
    local bundle_dir="$1"
    
    check_rsync
    check_rsync_configured
    
    log_info "Syncing from remote host..."
    
    if rsync -avz \
        --exclude="*.lock" \
        --exclude="*.tmp" \
        --exclude="/mailbox/" \
        "${RSYNC_HOST}:${RSYNC_PATH}/" "$bundle_dir/"; then
        log_success "Synced from ${RSYNC_HOST}:${RSYNC_PATH}/"
    else
        log_error "Sync failed"
        return 1
    fi
}

# Cheap fingerprint of remote state for the daemon's change-detection poll
# (lib/daemon.sh: check_remote_changed()). See backends/local.sh for the
# full contract this mirrors (sorted "name size mtime" lines, "EMPTY" for
# reachable-but-nothing-published, empty stdout + non-zero for unreachable).
#
# Deliberately does NOT call check_rsync/check_rsync_configured: those
# print multi-line setup instructions via log_* (which echo to *stdout*)
# and exit 1 outright, which would make the instructions themselves the
# fingerprint instead of tripping the indeterminate branch.
#
# One ssh round trip does both jobs at once: if bundles/ exists, list it
# (name/size/mtime, via the remote's own `stat`); either way, print a
# REMOTE_OK sentinel line once we know RSYNC_PATH itself is there, so an
# empty result can be told apart from a dead connection. bundles/ not
# existing yet (RSYNC_PATH exists, nothing published from any machine yet)
# is EMPTY; RSYNC_PATH itself missing, or the ssh call failing outright
# (bad host, auth, network), is unreachable -- matching what
# check_rsync_configured already treats as "cannot access remote host".
#
# Portability note: this assumes a GNU-userland remote (`stat -c`), same as
# the rest of this backend already does implicitly (its setup docs describe
# a Linux host/NAS); it is not adapted for a BSD/macOS remote.
backend_list() {
    command -v ssh >/dev/null 2>&1 || return 1

    local host="${RSYNC_HOST%%:*}"
    local remote_cmd
    remote_cmd="if [ -d '$RSYNC_PATH/bundles' ]; then \
        find '$RSYNC_PATH/bundles' -maxdepth 1 -name '*.bundle' \
            -exec stat -c '%n %s %Y' {} \; ; \
        echo REMOTE_OK; \
    elif [ -d '$RSYNC_PATH' ]; then \
        echo REMOTE_OK; \
    fi"

    local output
    output=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$host" "$remote_cmd" 2>/dev/null) || return 1

    case "$output" in
        *REMOTE_OK) ;;
        *) return 1 ;;
    esac

    local listing
    listing=$(printf '%s\n' "$output" | grep -v '^REMOTE_OK$')

    if [ -z "$listing" ]; then
        echo "EMPTY"
        return 0
    fi

    printf '%s\n' "$listing" | LC_ALL=C sort
}

backend_status() {
    check_rsync
    
    log_info "Backend: rsync"
    log_info "Remote: ${RSYNC_HOST}:${RSYNC_PATH}"
    
    if check_rsync_configured 2>/dev/null; then
        log_success "Remote host accessible"
        
        local bundles
        bundles="$(ssh "${RSYNC_HOST%%:*}" \
            "find ${RSYNC_PATH}/bundles -name '*.bundle' 2>/dev/null" || true)"
        if [ -n "$bundles" ]; then
            local count
            count="$(printf '%s\n' "$bundles" | grep -c . || true)"
            log_success "Reachable; $count machine bundle(s) present"
            printf '%s\n' "$bundles" | while read -r bundle; do
                [ -n "$bundle" ] || continue
                echo "    $(basename "$bundle" .bundle)"
            done
        else
            log_warn "No sync data found on remote"
        fi
    else
        log_warn "Remote host not accessible"
    fi
}

# --------------------------------------------------------------------------
# Session mailbox (send-session / inbox). Contract: docs/backends/custom.md,
# "Mailbox functions". Paths are relative to $RSYNC_PATH/mailbox and look like
# "<recipient>/<file>"; kirocrew-sync.sh only ever passes sanitized names
# ([A-Za-z0-9._-]), so single-quoting them for the remote shell is safe.

rsync_mailbox_root() {
    echo "$RSYNC_PATH/mailbox"
}

backend_mailbox_put() {
    local src="$1" rel="$2"
    check_rsync
    check_rsync_configured
    local host="${RSYNC_HOST%%:*}"
    local dest dir tmp
    dest="$(rsync_mailbox_root)/$rel"
    dir="$(dirname "$dest")"
    tmp="$dir/.$(basename "$dest").tmp"
    # Upload under a hidden name and rename on the remote, so a concurrent
    # inbox listing never picks up half a file.
    ssh "$host" "mkdir -p '$dir'" &&
        rsync -az "$src" "${RSYNC_HOST}:${tmp}" &&
        ssh "$host" "mv -f '$tmp' '$dest'"
}

backend_mailbox_get() {
    local rel="$1" dest="$2"
    check_rsync
    rsync -az "${RSYNC_HOST}:$(rsync_mailbox_root)/$rel" "$dest"
}

backend_mailbox_list() {
    local recipient="$1"
    command -v ssh >/dev/null 2>&1 || return 1
    local host="${RSYNC_HOST%%:*}"
    local dir
    dir="$(rsync_mailbox_root)/$recipient"
    local output
    output=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$host" \
        "if [ -d '$dir' ]; then \
            find '$dir' -maxdepth 1 -name '*.kcsession.json.gz' \
                -exec stat -c '%n %s' {} \; ; \
        fi; \
        [ -d '$RSYNC_PATH' ] && echo REMOTE_OK" 2>/dev/null) || return 1
    case "$output" in
        *REMOTE_OK) ;;
        *) return 1 ;;
    esac
    printf '%s\n' "$output" | grep -v '^REMOTE_OK$' | while read -r path size; do
        [ -n "$path" ] || continue
        echo "$(basename "$path") $size"
    done | LC_ALL=C sort
}

backend_mailbox_delete() {
    local rel="$1"
    local host="${RSYNC_HOST%%:*}"
    ssh "$host" "rm -f '$(rsync_mailbox_root)/$rel'"
}
