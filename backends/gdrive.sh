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

backend_status() {
    check_rclone
    
    log_info "Backend: Google Drive (via rclone)"
    log_info "Remote name: ${GDRIVE_REMOTE_NAME}"
    log_info "Remote directory: ${GDRIVE_SYNC_DIR}"
    
    if check_gdrive_configured 2>/dev/null; then
        log_success "Google Drive configured"
        
        # Check if sync directory exists
        if rclone lsd "${GDRIVE_REMOTE_NAME}:" 2>/dev/null | grep -q "${GDRIVE_SYNC_DIR}"; then
            log_info "\nRemote sync data:"
            
            # Get manifest info if exists
            local temp_manifest
            temp_manifest=$(mktemp)
            if rclone copyto "${GDRIVE_REMOTE_NAME}:${GDRIVE_SYNC_DIR}/manifest.json" "$temp_manifest" 2>/dev/null; then
                local remote_machine
                remote_machine=$(grep -o '"machine_id": "[^"]*"' "$temp_manifest" | cut -d'"' -f4)
                local remote_time
                remote_time=$(grep -o '"timestamp": "[^"]*"' "$temp_manifest" | cut -d'"' -f4)
                
                echo "  Last sync from: $remote_machine"
                echo "  Last sync time: $remote_time"
            else
                log_warn "No sync data found on remote"
            fi
            rm -f "$temp_manifest"
        else
            log_warn "Sync directory not found on remote (no data synced yet)"
        fi
    else
        log_warn "Google Drive not configured"
    fi
}
