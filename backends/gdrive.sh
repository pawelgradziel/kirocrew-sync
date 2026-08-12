#!/usr/bin/env bash
#
# Google Drive backend for KiroCrew Sync
# Uses rclone with Google Drive
#

# Configuration
GDRIVE_REMOTE_NAME="${GDRIVE_REMOTE_NAME:-kirocrew-gdrive}"
GDRIVE_SYNC_DIR="${GDRIVE_SYNC_DIR:-KiroCrew-Sync}"

# Check if rclone is installed
check_rclone() {
    if ! command -v rclone &> /dev/null; then
        log_error "rclone is not installed"
        log_info "Install rclone:"
        log_info "  macOS:   brew install rclone"
        log_info "  Linux:   curl https://rclone.org/install.sh | sudo bash"
        log_info "  Or see:  https://rclone.org/downloads/"
        exit 1
    fi
}

# Check if Google Drive remote is configured
check_gdrive_configured() {
    if ! rclone listremotes | grep -q "^${GDRIVE_REMOTE_NAME}:$"; then
        log_error "Google Drive remote '${GDRIVE_REMOTE_NAME}' not configured"
        log_info ""
        log_info "To configure Google Drive:"
        log_info ""
        log_info "1. Run: rclone config"
        log_info ""
        log_info "2. Choose: n (New remote)"
        log_info ""
        log_info "3. Name: ${GDRIVE_REMOTE_NAME}"
        log_info ""
        log_info "4. Storage type: drive (Google Drive)"
        log_info ""
        log_info "5. For scope, choose: drive (Full access)"
        log_info ""
        log_info "6. Leave blank for client_id and client_secret (uses rclone's)"
        log_info "   OR create your own OAuth app:"
        log_info ""
        log_info "   Creating your own OAuth credentials (recommended):"
        log_info "   a) Go to: https://console.cloud.google.com/apis/credentials"
        log_info "   b) Create a new project or select existing"
        log_info "   c) Click 'Create Credentials' > 'OAuth client ID'"
        log_info "   d) Application type: 'Desktop app'"
        log_info "   e) Copy the Client ID and Client Secret"
        log_info "   f) Enable Google Drive API:"
        log_info "      https://console.cloud.google.com/apis/library/drive.googleapis.com"
        log_info ""
        log_info "7. Follow the browser authorization flow"
        log_info ""
        log_info "8. Test with: rclone lsd ${GDRIVE_REMOTE_NAME}:"
        log_info ""
        log_info "Note: OAuth credentials are stored in:"
        log_info "  Linux:  ~/.config/rclone/rclone.conf"
        log_info "  macOS:  ~/.config/rclone/rclone.conf"
        log_info ""
        log_info "This file is already in .gitignore and won't be committed."
        log_info ""
        exit 1
    fi
}

backend_push() {
    local bundle_dir="$1"
    
    check_rclone
    check_gdrive_configured
    
    log_info "Uploading to Google Drive..."
    
    # Create remote directory if it doesn't exist
    rclone mkdir "${GDRIVE_REMOTE_NAME}:${GDRIVE_SYNC_DIR}"
    
    # Sync bundle to Google Drive
    # Using copy instead of sync to preserve local if remote is empty
    if rclone copy "$bundle_dir" "${GDRIVE_REMOTE_NAME}:${GDRIVE_SYNC_DIR}" \
        --progress \
        --checkers 8 \
        --transfers 4 \
        --delete-during; then
        log_success "Uploaded to Google Drive: ${GDRIVE_SYNC_DIR}/"
    else
        log_error "Upload failed"
        return 1
    fi
}

backend_pull() {
    local bundle_dir="$1"
    
    check_rclone
    check_gdrive_configured
    
    log_info "Downloading from Google Drive..."
    
    # Check if remote directory exists
    if ! rclone lsd "${GDRIVE_REMOTE_NAME}:" | grep -q "${GDRIVE_SYNC_DIR}"; then
        log_error "Remote directory '${GDRIVE_SYNC_DIR}' not found on Google Drive"
        log_info "Run: ./kirocrew-sync.sh push (from another machine)"
        return 1
    fi
    
    # Download from Google Drive
    if rclone copy "${GDRIVE_REMOTE_NAME}:${GDRIVE_SYNC_DIR}" "$bundle_dir" \
        --progress \
        --checkers 8 \
        --transfers 4; then
        log_success "Downloaded from Google Drive: ${GDRIVE_SYNC_DIR}/"
    else
        log_error "Download failed"
        return 1
    fi
}

# Cheap fingerprint of remote state for the daemon's change-detection poll
# (lib/daemon.sh: check_remote_changed()). See backends/local.sh for the
# full contract this mirrors (sorted "name size mtime" lines, "EMPTY" for
# reachable-but-nothing-published, empty stdout + non-zero for unreachable).
#
# Deliberately does NOT call check_rclone/check_gdrive_configured: those
# print multi-line setup instructions via log_* (which echo to *stdout*),
# and exit 1 outright. Called from $(backend_list ...) that's merely
# subshell-contained, not "clean failure" -- it would make the instructions
# themselves the fingerprint, and the fingerprint would look stable and
# "reachable" instead of tripping the indeterminate branch. Same command-v
# check check_rclone uses, just without the printing/exit side effects.
backend_list() {
    command -v rclone >/dev/null 2>&1 || return 1

    # rclone lsf's own "pst" format is exactly path/size/modtime -- no need
    # to shell out to `stat`, and modtime here is the object's remote
    # metadata, not this query's wall-clock time.
    local listing rc
    listing=$(rclone lsf "${GDRIVE_REMOTE_NAME}:${GDRIVE_SYNC_DIR}/bundles" \
        --files-only --include '*.bundle' \
        --format "pst" --separator " " 2>/dev/null)
    rc=$?

    if [ $rc -ne 0 ] || [ -z "$listing" ]; then
        # rclone lsf fails when the bundles/ (or GDRIVE_SYNC_DIR) path
        # doesn't exist yet, which is simply "nothing published yet" on a
        # remote that otherwise works fine (true before any machine's
        # first push). Disambiguate from a genuinely unreachable remote
        # (bad auth, network down, remote not configured) with a second,
        # cheap probe against the remote's root.
        if rclone lsd "${GDRIVE_REMOTE_NAME}:" >/dev/null 2>&1; then
            echo "EMPTY"
            return 0
        fi
        return 1
    fi

    printf '%s\n' "$listing" | LC_ALL=C sort
}

backend_status() {
    check_rclone
    
    log_info "Backend: Google Drive (via rclone)"
    log_info "Remote name: ${GDRIVE_REMOTE_NAME}"
    log_info "Remote directory: ${GDRIVE_SYNC_DIR}"
    
    if check_gdrive_configured 2>/dev/null; then
        log_success "Google Drive configured"
        
        if rclone lsd "${GDRIVE_REMOTE_NAME}:" 2>/dev/null | grep -q "${GDRIVE_SYNC_DIR}"; then
            local bundles
            bundles="$(rclone lsf "${GDRIVE_REMOTE_NAME}:${GDRIVE_SYNC_DIR}/bundles" \
                --include '*.bundle' 2>/dev/null || true)"
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
            log_warn "Sync directory not found on remote (no data synced yet)"
        fi
    else
        log_warn "Google Drive not configured"
    fi
}
