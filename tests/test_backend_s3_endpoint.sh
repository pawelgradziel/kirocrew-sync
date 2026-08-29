#!/usr/bin/env bash
#
# backends/s3.sh talks to any S3-compatible object store -- Cloudflare R2,
# MinIO, Backblaze B2 -- and not only to AWS itself, by passing
# --endpoint-url and --region when S3_ENDPOINT_URL / S3_REGION are set.
# Those two flags have to reach *every* `aws` invocation the backend makes:
# a call site that missed --endpoint-url would not fail loudly, it would
# quietly talk to real AWS with the store's credentials. That is what this
# suite pins down, in both directions:
#
#   - with neither variable set, the command lines are byte for byte the
#     ones this backend ran before either variable existed (asserted as
#     literal strings, not as "contains --profile"), so nothing changes for
#     an AWS user;
#   - with either variable set, the flag appears on every recorded call --
#     the probe in check_s3_configured, both `aws s3 sync`s, and all three
#     `aws s3 ls`s -- and never as an empty argument;
#   - backend_list keeps the fingerprint contract the daemon depends on
#     (see tests/test_backend_local_list.sh for the reference statement of
#     it: "EMPTY" when reachable and empty, sorted lines when bundles
#     exist, no stdout and non-zero when unreachable);
#   - the setup advice printed on failure matches the store actually
#     configured, instead of telling an R2 user to run `aws configure` and
#     `aws s3 mb` against AWS.
#
# Everything runs against a stub `aws` placed earlier on PATH, which records
# its full argv and emits canned output: no credentials, no network, and no
# real bucket. It follows that this suite proves the *command lines* are
# right, not that R2 accepts them -- see GOAL.md's "Never run end-to-end
# against a real remote backend".
#
# Unlike backends/local.sh, most of backends/s3.sh does call log_*, which
# kirocrew-sync.sh (not the backend) defines -- so, exactly as
# app/backend/backends.py does before running backend_status, the four
# helpers are stubbed here before the backend is sourced. Plain echo, no
# colours: the assertions below read the text.
#
# Run: ./tests/test_backend_s3_endpoint.sh
#

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0

ok()  { printf '  \033[0;32m✓\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf '  \033[0;31m✗\033[0m %s\n' "$1"; shift; for line in "$@"; do printf '      %s\n' "$line"; done; FAIL=$((FAIL + 1)); }

assert_eq() {
    local name="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then ok "$name"
    else bad "$name" "expected: $expected" "actual:   $actual"; fi
}

assert_contains() {
    local name="$1" haystack="$2" needle="$3"
    case "$haystack" in
        *"$needle"*) ok "$name" ;;
        *) bad "$name" "missing: $needle" "in:      $haystack" ;;
    esac
}

assert_absent() {
    local name="$1" haystack="$2" needle="$3"
    case "$haystack" in
        *"$needle"*) bad "$name" "unexpectedly present: $needle" "in: $haystack" ;;
        *) ok "$name" ;;
    esac
}

log_info()    { echo "$*"; }
log_success() { echo "$*"; }
log_warn()    { echo "$*"; }
log_error()   { echo "$*"; }

echo "backends/s3.sh: custom endpoint and region"
echo

# --- The stub aws -----------------------------------------------------------
# Records the full argv of every call, one call per line, then emits whatever
# the scenario asked for. Arguments are joined with single spaces: no fixture
# value used here contains a space, so a recorded line can be compared byte
# for byte with the command line the backend was supposed to run -- and an
# accidental empty argument (the failure mode of writing --endpoint-url
# "$S3_ENDPOINT_URL" inline with the variable unset) shows up in it as a
# double space rather than vanishing.
STUB_BIN="$WORK/bin"
mkdir -p "$STUB_BIN"
cat > "$STUB_BIN/aws" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "aws $*" >> "$AWS_STUB_CALLS"
case "$*" in
    *"/bundles/"*)
        if [ -n "${AWS_STUB_LISTING:-}" ]; then
            printf '%s\n' "$AWS_STUB_LISTING"
        fi
        ;;
esac
exit "${AWS_STUB_RC:-0}"
STUB
chmod +x "$STUB_BIN/aws"
export PATH="$STUB_BIN:$PATH"

export AWS_STUB_CALLS="$WORK/aws-calls.log"
export AWS_STUB_LISTING=""
export AWS_STUB_RC=0

reset_calls() { : > "$AWS_STUB_CALLS"; }
calls()       { cat "$AWS_STUB_CALLS"; }
call_count()  { grep -c . "$AWS_STUB_CALLS" 2>/dev/null || echo 0; }

# Fixture config, in the shape a config.sh would set it.
export S3_BUCKET="kirocrew-state"
export S3_PREFIX="kirocrew-sync"
export AWS_PROFILE="default"
BUNDLE_DIR="$WORK/bundle"
mkdir -p "$BUNDLE_DIR"

# kirocrew-sync.sh sources config.sh and *then* the backend script, so the
# backend reads these variables once, at source time. Each scenario below
# therefore sets the environment and re-sources, rather than mutating the
# backend's own variables behind its back.
source_backend() {
    # shellcheck source=/dev/null
    source "$REPO_DIR/backends/s3.sh"
}

# Every line of a recorded run must carry $2; report how many did not.
lines_missing() {
    local needle="$1" missing=0 line
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        case "$line" in
            *"$needle"*) ;;
            *) missing=$((missing + 1)) ;;
        esac
    done < "$AWS_STUB_CALLS"
    echo "$missing"
}

# --- Scenario 1: neither variable set -> today's exact command lines --------
# The compatibility guarantee. If any assertion here fails, an existing AWS
# user's commands changed, whatever else the new support does.
unset S3_ENDPOINT_URL S3_REGION
source_backend; rc=$?
assert_eq "sourcing with neither variable set succeeds under set -u" "0" "$rc"

reset_calls
backend_push "$BUNDLE_DIR" >/dev/null
recorded="$(calls)"
assert_eq "push probes the bucket exactly as before" \
    "aws s3 ls s3://kirocrew-state --profile default" \
    "$(sed -n 1p "$AWS_STUB_CALLS")"
assert_eq "push uploads exactly as before" \
    "aws s3 sync $BUNDLE_DIR s3://kirocrew-state/kirocrew-sync/ --profile default --delete --exclude *.lock --exclude *.tmp" \
    "$(sed -n 2p "$AWS_STUB_CALLS")"

reset_calls
backend_pull "$BUNDLE_DIR" >/dev/null
assert_eq "pull downloads exactly as before" \
    "aws s3 sync s3://kirocrew-state/kirocrew-sync/ $BUNDLE_DIR --profile default --exclude *.lock --exclude *.tmp" \
    "$(sed -n 2p "$AWS_STUB_CALLS")"

reset_calls
backend_list >/dev/null
assert_eq "backend_list lists exactly as before" \
    "aws s3 ls s3://kirocrew-state/kirocrew-sync/bundles/ --profile default" \
    "$(sed -n 1p "$AWS_STUB_CALLS")"

reset_calls
backend_status >/dev/null
assert_eq "status lists exactly as before" \
    "aws s3 ls s3://kirocrew-state/kirocrew-sync/bundles/ --profile default" \
    "$(sed -n 2p "$AWS_STUB_CALLS")"

# The same guarantee stated negatively, across one run of all four entry
# points: nothing anywhere gained a flag, and nothing gained an empty
# argument (which a double space would betray).
reset_calls
backend_push "$BUNDLE_DIR" >/dev/null
backend_pull "$BUNDLE_DIR" >/dev/null
backend_list >/dev/null
backend_status >/dev/null
recorded="$(calls)"
assert_absent "no --endpoint-url anywhere when S3_ENDPOINT_URL is unset" "$recorded" "--endpoint-url"
assert_absent "no --region anywhere when S3_REGION is unset" "$recorded" "--region"
assert_absent "no empty argument is passed when either variable is unset" "$recorded" "  "

# --- Scenario 2: S3_ENDPOINT_URL set (Cloudflare R2) ------------------------
export S3_ENDPOINT_URL="https://abc123.r2.cloudflarestorage.com"
unset S3_REGION
source_backend

reset_calls
backend_push "$BUNDLE_DIR" >/dev/null
backend_pull "$BUNDLE_DIR" >/dev/null
backend_list >/dev/null
backend_status >/dev/null

assert_eq "all seven aws calls are recorded (2 push, 2 pull, 1 list, 2 status)" \
    "7" "$(call_count)"
assert_eq "every aws call carries --endpoint-url" \
    "0" "$(lines_missing "--endpoint-url https://abc123.r2.cloudflarestorage.com")"
assert_absent "--region is still absent when only the endpoint is set" "$(calls)" "--region"
assert_eq "the endpoint lands where --profile always was, not at the end" \
    "aws s3 sync $BUNDLE_DIR s3://kirocrew-state/kirocrew-sync/ --profile default --endpoint-url https://abc123.r2.cloudflarestorage.com --delete --exclude *.lock --exclude *.tmp" \
    "$(sed -n 2p "$AWS_STUB_CALLS")"

# --- Scenario 3: S3_REGION set --------------------------------------------
unset S3_ENDPOINT_URL
export S3_REGION="auto"
source_backend

reset_calls
backend_push "$BUNDLE_DIR" >/dev/null
backend_pull "$BUNDLE_DIR" >/dev/null
backend_list >/dev/null
backend_status >/dev/null

assert_eq "every aws call carries --region" "0" "$(lines_missing "--region auto")"
assert_absent "--endpoint-url is absent when only the region is set" "$(calls)" "--endpoint-url"

# --- Scenario 4: both set -------------------------------------------------
export S3_ENDPOINT_URL="https://abc123.r2.cloudflarestorage.com"
export S3_REGION="auto"
source_backend

reset_calls
backend_push "$BUNDLE_DIR" >/dev/null
backend_pull "$BUNDLE_DIR" >/dev/null
backend_list >/dev/null
backend_status >/dev/null

assert_eq "every aws call carries both flags" \
    "0" "$(lines_missing "--profile default --endpoint-url https://abc123.r2.cloudflarestorage.com --region auto")"
# Line 4 of this run: push probes (1) and uploads (2), pull probes (3) and
# downloads (4).
assert_eq "the pull command line is exactly the R2 one" \
    "aws s3 sync s3://kirocrew-state/kirocrew-sync/ $BUNDLE_DIR --profile default --endpoint-url https://abc123.r2.cloudflarestorage.com --region auto --exclude *.lock --exclude *.tmp" \
    "$(sed -n 4p "$AWS_STUB_CALLS")"

out="$(backend_status 2>&1)"
assert_contains "status names the configured endpoint" "$out" \
    "Endpoint: https://abc123.r2.cloudflarestorage.com"
assert_contains "status names the configured region" "$out" "Region: auto"
assert_absent "status does not claim to be AWS S3 when it is not" "$out" "Backend: AWS S3"

# --- Scenario 5: backend_list keeps its contract, endpoint or not ----------
# Same three outcomes tests/test_backend_local_list.sh pins for the reference
# implementation. Run with the endpoint set, since that is the configuration
# that did not exist before and could plausibly break the parsing.
export AWS_STUB_RC=0
export AWS_STUB_LISTING=""
reset_calls
out="$(backend_list)"; rc=$?
assert_eq "reachable-but-empty prefix exits 0" "0" "$rc"
assert_eq "reachable-but-empty prefix prints the EMPTY sentinel" "EMPTY" "$out"

# `aws s3 ls` on a prefix also emits PRE lines for sub-prefixes and rows for
# non-bundle objects; both must be filtered out. The two bundles are fed in
# reverse order to prove the output is sorted rather than merely echoed.
export AWS_STUB_LISTING="                           PRE nested/
2026-08-27 09:15:04       4096 machine-b.bundle
2026-08-29 12:00:01       2048 machine-a.bundle
2026-08-29 12:00:03        128 manifest.json"
out="$(backend_list)"; rc=$?
assert_eq "a listing with bundles exits 0" "0" "$rc"
assert_eq "bundles are reported sorted, as \"name size date time\"" \
    "$(printf 'machine-a.bundle 2048 2026-08-29 12:00:01\nmachine-b.bundle 4096 2026-08-27 09:15:04')" \
    "$out"
assert_absent "non-bundle keys are not part of the fingerprint" "$out" "manifest.json"
assert_absent "sub-prefix lines are not part of the fingerprint" "$out" "nested"

second="$(backend_list)"
assert_eq "an unchanged remote fingerprints identically across two calls" "$out" "$second"

export AWS_STUB_RC=1
out="$(backend_list)"; rc=$?
assert_eq "an unreachable bucket exits non-zero" "1" "$rc"
assert_eq "an unreachable bucket prints nothing" "" "$out"
export AWS_STUB_RC=0
export AWS_STUB_LISTING=""

# --- Scenario 6: the setup advice matches the store configured -------------
# check_s3_configured exits 1 on failure; the command substitution contains
# that exit, which is also how backend_status survives calling it.
export AWS_STUB_RC=1

export S3_ENDPOINT_URL="https://abc123.r2.cloudflarestorage.com"
export S3_REGION="auto"
source_backend
advice="$(check_s3_configured 2>&1)"
assert_contains "R2 advice names the endpoint in use" "$advice" \
    "https://abc123.r2.cloudflarestorage.com"
assert_contains "R2 advice creates the bucket through that endpoint" "$advice" \
    "aws s3 mb s3://kirocrew-state --profile default --endpoint-url https://abc123.r2.cloudflarestorage.com --region auto"
assert_absent "R2 advice does not push the AWS-only versioning call" "$advice" \
    "put-bucket-versioning"
assert_contains "R2 advice points at config.sh" "$advice" "config.sh"

unset S3_ENDPOINT_URL S3_REGION
source_backend
advice="$(check_s3_configured 2>&1)"
assert_contains "AWS advice still creates the bucket the AWS way" "$advice" \
    "aws s3 mb s3://kirocrew-state --profile default"
assert_contains "AWS advice still recommends versioning" "$advice" "put-bucket-versioning"
assert_contains "AWS advice points at config.sh, not at editing the backend" \
    "$advice" "config.sh"
assert_absent "the stale \"edit backends/s3.sh\" instruction is gone" "$advice" \
    "Update backends/s3.sh"
export AWS_STUB_RC=0

echo
if [ "$FAIL" -gt 0 ]; then
    printf '\033[0;31m✗\033[0m %d passed, %d failed\n' "$PASS" "$FAIL"
    exit 1
fi
printf '\033[0;32m✓\033[0m %d passed\n' "$PASS"
