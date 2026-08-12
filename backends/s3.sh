#!/usr/bin/env bash
#
# AWS S3 backend for KiroCrew Sync
# Uses AWS CLI
#

# Configuration
S3_BUCKET="${S3_BUCKET:-your-bucket-name}"
S3_PREFIX="${S3_PREFIX:-kirocrew-sync}"
AWS_PROFILE="${AWS_PROFILE:-default}"

check_aws_cli() {
    if ! command -v aws &> /dev/null; then
        log_error "AWS CLI is not installed"
        log_info "Install AWS CLI:"
        log_info "  macOS:   brew install awscli"
        log_info "  Linux:   pip install awscli"
        log_info "  Or see:  https://aws.amazon.com/cli/"
        exit 1
    fi
}

check_s3_configured() {
    if ! aws s3 ls "s3://${S3_BUCKET}" --profile "$AWS_PROFILE" &>/dev/null; then
        log_error "Cannot access S3 bucket: ${S3_BUCKET}"
        log_info ""
        log_info "To configure S3:"
        log_info ""
        log_info "1. Configure AWS credentials:"
        log_info "   aws configure --profile $AWS_PROFILE"
        log_info ""
        log_info "2. Create S3 bucket:"
        log_info "   aws s3 mb s3://${S3_BUCKET} --profile $AWS_PROFILE"
        log_info ""
        log_info "3. Enable versioning (recommended):"
        log_info "   aws s3api put-bucket-versioning \\"
        log_info "     --bucket ${S3_BUCKET} \\"
        log_info "     --versioning-configuration Status=Enabled \\"
        log_info "     --profile $AWS_PROFILE"
        log_info ""
        log_info "4. Update backends/s3.sh with your bucket name"
        log_info ""
        exit 1
    fi
}

backend_push() {
    local bundle_dir="$1"
    
    check_aws_cli
    check_s3_configured
    
    log_info "Uploading to S3..."
    
    if aws s3 sync "$bundle_dir" "s3://${S3_BUCKET}/${S3_PREFIX}/" \
        --profile "$AWS_PROFILE" \
        --delete \
        --exclude "*.lock" \
        --exclude "*.tmp"; then
        log_success "Uploaded to S3: s3://${S3_BUCKET}/${S3_PREFIX}/"
    else
        log_error "Upload failed"
        return 1
    fi
}

backend_pull() {
    local bundle_dir="$1"
    
    check_aws_cli
    check_s3_configured
    
    log_info "Downloading from S3..."
    
    if aws s3 sync "s3://${S3_BUCKET}/${S3_PREFIX}/" "$bundle_dir" \
        --profile "$AWS_PROFILE" \
        --exclude "*.lock" \
        --exclude "*.tmp"; then
        log_success "Downloaded from S3: s3://${S3_BUCKET}/${S3_PREFIX}/"
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
# Deliberately does NOT call check_aws_cli/check_s3_configured: those print
# multi-line setup instructions via log_* (which echo to *stdout*) and
# exit 1 outright, which would make the instructions themselves the
# fingerprint instead of tripping the indeterminate branch. Same
# command -v check check_aws_cli uses, just without the printing/exit.
backend_list() {
    command -v aws >/dev/null 2>&1 || return 1

    # `aws s3 ls` on a prefix already gives name/size/date/time per object
    # in one call (same shape backend_status already parses below) --
    # date/time here are the object's own LastModified, not query time.
    # Unlike a real filesystem, S3 has no directories to be "missing": `ls`
    # on a bucket that exists but has zero matching keys still exits 0 with
    # empty output, so a bare non-zero exit here is unambiguously "bucket
    # unreachable" (bad credentials, wrong bucket, network down) and empty
    # output alone (with rc=0) is unambiguously "reachable, nothing
    # published yet" -- no separate reachability probe needed.
    local listing
    listing=$(aws s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}/bundles/" \
        --profile "$AWS_PROFILE" 2>/dev/null) || return 1

    local bundles
    bundles=$(printf '%s\n' "$listing" | awk '/\.bundle$/ {print $4, $3, $1, $2}')

    if [ -z "$bundles" ]; then
        echo "EMPTY"
        return 0
    fi

    printf '%s\n' "$bundles" | LC_ALL=C sort
}

backend_status() {
    check_aws_cli
    
    log_info "Backend: AWS S3"
    log_info "Bucket: s3://${S3_BUCKET}/${S3_PREFIX}/"
    log_info "Profile: $AWS_PROFILE"
    
    if check_s3_configured 2>/dev/null; then
        log_success "S3 bucket accessible"
        
        local listing
        listing="$(aws s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}/bundles/" \
            --profile "$AWS_PROFILE" 2>/dev/null || true)"
        local bundles
        bundles="$(printf '%s\n' "$listing" | awk '/\.bundle$/ {print $NF}')"
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
        log_warn "S3 bucket not accessible"
    fi
}
