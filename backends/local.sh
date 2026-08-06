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
