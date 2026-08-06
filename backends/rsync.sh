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
    
    if rsync -avz --delete \
        --exclude="*.lock" \
        --exclude="*.tmp" \
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
        "${RSYNC_HOST}:${RSYNC_PATH}/" "$bundle_dir/"; then
        log_success "Synced from ${RSYNC_HOST}:${RSYNC_PATH}/"
    else
        log_error "Sync failed"
        return 1
    fi
}

backend_status() {
    check_rsync
    
    log_info "Backend: rsync"
    log_info "Remote: ${RSYNC_HOST}:${RSYNC_PATH}"
    
    if check_rsync_configured 2>/dev/null; then
        log_success "Remote host accessible"
        
        # Check if manifest exists
        if ssh "${RSYNC_HOST%%:*}" "test -f ${RSYNC_PATH}/manifest.json" 2>/dev/null; then
            log_info "\nRemote sync data found"
            
            # Get remote machine info
            local manifest_content
            manifest_content=$(ssh "${RSYNC_HOST%%:*}" "cat ${RSYNC_PATH}/manifest.json" 2>/dev/null)
            
            if [ -n "$manifest_content" ]; then
                local remote_machine
                remote_machine=$(echo "$manifest_content" | grep -o '"machine_id": "[^"]*"' | cut -d'"' -f4)
                local remote_time
                remote_time=$(echo "$manifest_content" | grep -o '"timestamp": "[^"]*"' | cut -d'"' -f4)
                
                echo "  Last sync from: $remote_machine"
                echo "  Last sync time: $remote_time"
            fi
        else
            log_warn "No sync data found on remote"
        fi
    else
        log_warn "Remote host not accessible"
    fi
}
