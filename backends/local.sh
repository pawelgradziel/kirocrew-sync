#!/usr/bin/env bash
#
# Local directory backend for KiroCrew Sync
#
# Points at any directory that reaches your other machines by other means:
# a Dropbox/Syncthing/iCloud folder, a mounted NAS share, or a USB drive.
# Also the backend the test suite runs against.
#

LOCAL_SYNC_DIR="${LOCAL_SYNC_DIR:-$HOME/Dropbox/KiroCrew-Sync}"

check_local_configured() {
    local parent
    parent="$(dirname "$LOCAL_SYNC_DIR")"
    if [ ! -d "$parent" ]; then
        log_error "Parent directory does not exist: $parent"
        log_info ""
        log_info "To configure the local backend, set LOCAL_SYNC_DIR to a"
        log_info "directory that is shared with your other machines:"
        log_info "  export LOCAL_SYNC_DIR=\"\$HOME/Dropbox/KiroCrew-Sync\""
        log_info ""
        exit 1
    fi
}

backend_push() {
    local bundle_dir="$1"

    check_local_configured
    mkdir -p "$LOCAL_SYNC_DIR"

    if command -v rsync &> /dev/null; then
        rsync -a --delete "$bundle_dir/" "$LOCAL_SYNC_DIR/"
    else
        rm -rf "${LOCAL_SYNC_DIR:?}/bundles"
        cp -R "$bundle_dir/." "$LOCAL_SYNC_DIR/"
    fi
    log_success "Published to $LOCAL_SYNC_DIR/"
}

backend_pull() {
    local bundle_dir="$1"

    if [ ! -d "$LOCAL_SYNC_DIR" ]; then
        return 1
    fi
    mkdir -p "$bundle_dir"
    if command -v rsync &> /dev/null; then
        rsync -a "$LOCAL_SYNC_DIR/" "$bundle_dir/"
    else
        cp -R "$LOCAL_SYNC_DIR/." "$bundle_dir/"
    fi
}

# Cheap fingerprint of remote state for the daemon's change-detection poll
# (lib/daemon.sh: check_remote_changed). Contract: print a sorted, stable
# listing that changes if and only if the remote's bundle set or content
# changes, and fail (non-zero, no stdout) when the remote can't be reached
# at all -- see docs/daemon.md and docs/backends/custom.md for the full
# contract this and the other three backends share.
#
# Reachability mirrors check_local_configured(): the *parent* of
# LOCAL_SYNC_DIR must exist (that's the shared folder root -- Dropbox,
# a mounted NAS, a USB drive -- being present at all). LOCAL_SYNC_DIR
# itself, and its bundles/ subdirectory, are created lazily by the first
# ever backend_push, so their absence is "reachable, nothing published
# yet", not "unreachable" -- printing the literal sentinel "EMPTY" for
# that case keeps it distinct from the unreachable case (empty stdout),
# which is the exact distinction check_remote_changed() depends on.
backend_list() {
    local parent
    parent="$(dirname "$LOCAL_SYNC_DIR")"
    if [ ! -d "$parent" ]; then
        return 1
    fi

    local bundles_dir="$LOCAL_SYNC_DIR/bundles"
    if [ ! -d "$bundles_dir" ]; then
        echo "EMPTY"
        return 0
    fi

    # name + size + mtime per bundle, not a content hash: cheap (this runs
    # on every daemon poll), and every backend_push rewrites the file so
    # mtime alone already changes on republish. mtime is the *file's* own
    # modification time (preserved by `rsync -a`, i.e. data from the
    # remote), never the time of this query -- a query timestamp would make
    # the fingerprint change on every single poll and defeat the point.
    local entries=""
    local bundle size mtime
    shopt -s nullglob
    for bundle in "$bundles_dir"/*.bundle; do
        if [[ "$OSTYPE" == "darwin"* ]]; then
            size=$(stat -f %z "$bundle" 2>/dev/null || echo 0)
            mtime=$(stat -f %m "$bundle" 2>/dev/null || echo 0)
        else
            size=$(stat -c %s "$bundle" 2>/dev/null || echo 0)
            mtime=$(stat -c %Y "$bundle" 2>/dev/null || echo 0)
        fi
        entries="${entries}$(basename "$bundle") ${size} ${mtime}"$'\n'
    done
    shopt -u nullglob

    if [ -z "$entries" ]; then
        echo "EMPTY"
        return 0
    fi

    # Sort so enumeration order (not guaranteed stable by the filesystem or
    # across bash versions) never makes an unchanged remote look changed.
    # LC_ALL=C keeps ordering independent of the machine's locale.
    printf '%s' "$entries" | LC_ALL=C sort
}

backend_status() {
    log_info "Backend: local directory"
    log_info "Path: $LOCAL_SYNC_DIR"

    if [ -d "$LOCAL_SYNC_DIR" ]; then
        local count
        count="$(find "$LOCAL_SYNC_DIR/bundles" -name '*.bundle' 2>/dev/null | wc -l | tr -d ' ')"
        log_success "Reachable; $count machine bundle(s) present"
        find "$LOCAL_SYNC_DIR/bundles" -name '*.bundle' 2>/dev/null \
            | while read -r bundle; do
                echo "    $(basename "$bundle" .bundle)"
            done
    else
        log_warn "Not created yet (nothing published)"
    fi
}
