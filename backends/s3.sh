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

backend_status() {
    check_aws_cli
    
    log_info "Backend: AWS S3"
    log_info "Bucket: s3://${S3_BUCKET}/${S3_PREFIX}/"
    log_info "Profile: $AWS_PROFILE"
    
    if check_s3_configured 2>/dev/null; then
        log_success "S3 bucket accessible"
        
        # Check if manifest exists
        if aws s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}/manifest.json" --profile "$AWS_PROFILE" &>/dev/null; then
            log_info "\nRemote sync data found"
            
            # Get last modified time
            local last_modified
            last_modified=$(aws s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}/manifest.json" --profile "$AWS_PROFILE" | awk '{print $1, $2}')
            echo "  Last modified: $last_modified"
        else
            log_warn "No sync data found on remote"
        fi
    else
        log_warn "S3 bucket not accessible"
    fi
}
