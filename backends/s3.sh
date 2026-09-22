#!/usr/bin/env bash
#
# AWS S3 backend for KiroCrew Sync
# Uses AWS CLI
#
# Also drives S3-compatible object stores -- Cloudflare R2, MinIO, Backblaze
# B2 -- which speak the same S3 API through this same `aws` CLI and differ
# only in where the request is sent and what region name they accept. See
# S3_ENDPOINT_URL / S3_REGION below and docs/backends/s3.md.
#

# Configuration
S3_BUCKET="${S3_BUCKET:-your-bucket-name}"
S3_PREFIX="${S3_PREFIX:-kirocrew-sync}"
AWS_PROFILE="${AWS_PROFILE:-default}"
# Empty (the default) means plain AWS S3: the corresponding flag is then not
# passed at all, so nothing about the commands this file runs changes.
# Cloudflare R2: S3_ENDPOINT_URL="https://<account-id>.r2.cloudflarestorage.com"
# and S3_REGION="auto", the region R2 documents for its S3 API.
S3_ENDPOINT_URL="${S3_ENDPOINT_URL:-}"
S3_REGION="${S3_REGION:-}"

# The arguments every `aws` call in this file carries, assembled in exactly
# one place. Five call sites need them -- the bucket probe in
# check_s3_configured (which backend_push, backend_pull and backend_status
# all run first), the two `aws s3 sync`s, and the listings in backend_list
# and backend_status. Hand-threading the flags into each is the kind of
# duplicate list that drifts, and a call site that missed --endpoint-url
# would not fail loudly -- it would quietly talk to real AWS instead of the
# store the user configured.
#
# An array, appended to conditionally, so an unset variable contributes *no*
# argument at all: an inline --endpoint-url "$S3_ENDPOINT_URL" would pass an
# empty string as the endpoint whenever the variable is unset, which the CLI
# rejects rather than ignores.
#
# Lands in the global AWS_S3_ARGS rather than being echoed, because echoing
# would lose the argument boundaries -- and because check_s3_configured()
# reuses it to print setup commands carrying the same flags this backend
# actually uses, instead of keeping a second, drifting copy of them.
aws_s3_args() {
    AWS_S3_ARGS=(--profile "$AWS_PROFILE")
    if [ -n "$S3_ENDPOINT_URL" ]; then
        AWS_S3_ARGS+=(--endpoint-url "$S3_ENDPOINT_URL")
    fi
    if [ -n "$S3_REGION" ]; then
        AWS_S3_ARGS+=(--region "$S3_REGION")
    fi
}

# Runs `aws s3 ...` with those arguments. Operands come first, then the
# common arguments, then anything after a literal `--`:
#
#   aws_s3 sync "$dir" "s3://b/p/" -- --delete --exclude "*.lock"
#   -> aws s3 sync "$dir" "s3://b/p/" --profile "$AWS_PROFILE" --delete ...
#
# The separator exists so the common arguments land exactly where each call
# site used to hand-write --profile: with S3_ENDPOINT_URL and S3_REGION
# unset the command line is then identical, argument for argument, to the
# one this backend ran before either variable existed. That is not a
# stylistic point -- tests/test_backend_s3_endpoint.sh asserts those exact
# command lines, so an AWS user's behavior cannot change under them.
aws_s3() {
    local -a operands=()
    while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
        operands+=("$1")
        shift
    done
    if [ "$#" -gt 0 ]; then
        shift
    fi

    aws_s3_args
    aws s3 "${operands[@]}" "${AWS_S3_ARGS[@]}" "$@"
}

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
    if aws_s3 ls "s3://${S3_BUCKET}" &>/dev/null; then
        return 0
    fi

    # Rendered from the same array the failing call itself used, so every
    # command suggested below carries the endpoint and region this backend
    # is actually configured with. Pointed at a custom endpoint, the AWS
    # advice is not merely incomplete but wrong: `aws configure` would be
    # storing the wrong provider's keys, and an `aws s3 mb` without
    # --endpoint-url would try to create the bucket in real AWS.
    aws_s3_args

    log_error "Cannot access S3 bucket: ${S3_BUCKET}"
    log_info ""

    if [ -n "$S3_ENDPOINT_URL" ]; then
        log_info "Endpoint: ${S3_ENDPOINT_URL}"
        log_info "(an S3-compatible store, not AWS S3)"
        log_info ""
        log_info "To configure it:"
        log_info ""
        log_info "1. Put that store's own access key and secret -- not AWS"
        log_info "   credentials -- in the profile this backend uses:"
        log_info "   aws configure --profile $AWS_PROFILE"
        log_info ""
        log_info "2. Create the bucket, in the provider's console or with:"
        log_info "   aws s3 mb s3://${S3_BUCKET} ${AWS_S3_ARGS[*]}"
        log_info ""
        log_info "3. Versioning and retention are set in the provider's own"
        log_info "   console -- not every store implements the s3api calls"
        log_info "   AWS does."
        log_info ""
        log_info "4. Keep S3_BUCKET, S3_ENDPOINT_URL and S3_REGION in"
        log_info "   config.sh (see config.sh.example). Cloudflare R2 needs"
        log_info "   S3_REGION=auto; see docs/backends/s3.md"
    else
        log_info "To configure S3:"
        log_info ""
        log_info "1. Configure AWS credentials:"
        log_info "   aws configure --profile $AWS_PROFILE"
        log_info ""
        log_info "2. Create S3 bucket:"
        log_info "   aws s3 mb s3://${S3_BUCKET} ${AWS_S3_ARGS[*]}"
        log_info ""
        log_info "3. Enable versioning (recommended):"
        log_info "   aws s3api put-bucket-versioning \\"
        log_info "     --bucket ${S3_BUCKET} \\"
        log_info "     --versioning-configuration Status=Enabled \\"
        log_info "     --profile $AWS_PROFILE"
        log_info ""
        log_info "4. Set S3_BUCKET (and S3_PREFIX, AWS_PROFILE) in config.sh"
        log_info "   -- see config.sh.example. The environment and config.sh"
        log_info "   are both read before this file's own defaults, so there"
        log_info "   is nothing to edit in backends/s3.sh."
        log_info ""
        log_info "   On Cloudflare R2 or another S3-compatible store, set"
        log_info "   S3_ENDPOINT_URL and S3_REGION too -- docs/backends/s3.md"
    fi

    log_info ""
    exit 1
}

backend_push() {
    local bundle_dir="$1"
    
    check_aws_cli
    check_s3_configured
    
    log_info "Uploading to S3..."

    # mailbox/ holds send-session bundles, not sync state. `aws s3 sync
    # --delete` never deletes an excluded key, so this keeps them alive.
    if aws_s3 sync "$bundle_dir" "s3://${S3_BUCKET}/${S3_PREFIX}/" -- \
        --delete \
        --exclude "*.lock" \
        --exclude "*.tmp" \
        --exclude "mailbox/*"; then
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
    
    if aws_s3 sync "s3://${S3_BUCKET}/${S3_PREFIX}/" "$bundle_dir" -- \
        --exclude "*.lock" \
        --exclude "*.tmp" \
        --exclude "mailbox/*"; then
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
    # unreachable" (bad credentials, wrong bucket, wrong endpoint, network
    # down) and empty output alone (with rc=0) is unambiguously "reachable,
    # nothing published yet" -- no separate reachability probe needed.
    local listing
    listing=$(aws_s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}/bundles/" 2>/dev/null) || return 1

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
    
    if [ -n "$S3_ENDPOINT_URL" ]; then
        log_info "Backend: S3-compatible store"
        log_info "Endpoint: $S3_ENDPOINT_URL"
    else
        log_info "Backend: AWS S3"
    fi
    log_info "Bucket: s3://${S3_BUCKET}/${S3_PREFIX}/"
    log_info "Profile: $AWS_PROFILE"
    if [ -n "$S3_REGION" ]; then
        log_info "Region: $S3_REGION"
    fi
    
    if check_s3_configured 2>/dev/null; then
        log_success "S3 bucket accessible"
        
        local listing
        listing="$(aws_s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}/bundles/" 2>/dev/null || true)"
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

# --------------------------------------------------------------------------
# Session mailbox (send-session / inbox). Contract: docs/backends/custom.md,
# "Mailbox functions". Keys live under s3://$S3_BUCKET/$S3_PREFIX/mailbox/ and
# the relative path is "<recipient>/<file>". Every call goes through aws_s3,
# so the endpoint and region settings apply here exactly as they do to sync.

s3_mailbox_url() {
    echo "s3://${S3_BUCKET}/${S3_PREFIX}/mailbox/$1"
}

backend_mailbox_put() {
    local src="$1" rel="$2"
    check_aws_cli
    check_s3_configured
    # A PUT is atomic in S3: a lister sees the whole object or nothing.
    aws_s3 cp "$src" "$(s3_mailbox_url "$rel")" -- --only-show-errors
}

backend_mailbox_get() {
    local rel="$1" dest="$2"
    check_aws_cli
    aws_s3 cp "$(s3_mailbox_url "$rel")" "$dest" -- --only-show-errors
}

# `aws s3 ls` on a prefix with no keys is not a reliable reachability signal
# (CLI versions differ on its exit status), so reachability is decided by the
# same bucket probe check_s3_configured uses, and an empty or failed listing
# after that is simply "nothing waiting".
backend_mailbox_list() {
    local recipient="$1"
    command -v aws >/dev/null 2>&1 || return 1
    aws_s3 ls "s3://${S3_BUCKET}" >/dev/null 2>&1 || return 1
    local listing
    listing=$(aws_s3 ls "$(s3_mailbox_url "$recipient/")" 2>/dev/null || true)
    printf '%s\n' "$listing" \
        | awk '$4 ~ /\.kcsession\.json\.gz$/ {print $4, $3}' \
        | LC_ALL=C sort
}

backend_mailbox_delete() {
    local rel="$1"
    aws_s3 rm "$(s3_mailbox_url "$rel")" -- --only-show-errors
}
