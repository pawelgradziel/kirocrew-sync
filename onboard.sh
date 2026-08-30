#!/usr/bin/env bash
#
# One-command onboarding for an R2 / S3-compatible sync mesh.
#
# Usage:
#   ./onboard.sh                       Interactive: discover, configure, verify
#   ./onboard.sh --scope both          Also write the team-scope config
#   ./onboard.sh --account-id <id>     Skip wrangler discovery
#   ./onboard.sh --sync | --no-sync    Force / skip the first sync at the end
#   ./onboard.sh --help                Show this help
#
# Turns the manual Setup steps (init, hand-edit config.sh, mint an R2 token,
# aws configure, remember S3_REGION=auto) into one run. OAuth is used
# everywhere Cloudflare allows it:
#
#   - account discovery and bucket checks ride wrangler's OAuth session
#     (`wrangler login` is a browser click, no keys typed);
#   - the R2 S3 key pair itself CANNOT be minted over OAuth - wrangler's
#     token gets HTTP 403 from the token-management API (verified
#     2026-08-30), and the S3 protocol only authenticates with SigV4 keys.
#     That one step stays in the dashboard, but this script opens the exact
#     page, says exactly what to click, takes the two pastes itself, and
#     stores them with `aws configure set` - the secret is never echoed.
#
# Safe to re-run: an already-working credential profile is detected and kept,
# an existing config file is only rewritten when its content would change
# (the old one is backed up beside it first), and every step that is already
# done says so and moves on.
#
# What it does, in order:
#   1. prereqs (git, python3, aws)          4. credentials (probe, else mint)
#   2. config file(s) for the chosen scope  5. init + doctor + status
#   3. account id -> endpoint (wrangler)    6. optional first sync
#
# The prefixes are fixed to the pair the container peer uses
# (sync/personal + sync/team, see kirocrew-at-cloudflare's
# container/sync-*.config.sh) - the engine's own scope-collision guard
# refuses a mismatched pair anyway, so there is nothing to choose.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}ℹ${NC} $*"; }
log_success() { echo -e "${GREEN}✓${NC} $*"; }
log_warn()    { echo -e "${YELLOW}⚠${NC} $*"; }
log_error()   { echo -e "${RED}✗${NC} $*"; }

usage() { sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'; }

BUCKET="kirocrew-state"
PROFILE="r2"
SCOPE="personal"
REGION="auto"
ACCOUNT_ID=""
ENDPOINT=""
DO_SYNC="ask"

while [ $# -gt 0 ]; do
    case "$1" in
        --bucket)     BUCKET="$2"; shift 2 ;;
        --profile)    PROFILE="$2"; shift 2 ;;
        --scope)      SCOPE="$2"; shift 2 ;;
        --region)     REGION="$2"; shift 2 ;;
        --account-id) ACCOUNT_ID="$2"; shift 2 ;;
        --endpoint)   ENDPOINT="$2"; shift 2 ;;
        --sync)       DO_SYNC="yes"; shift ;;
        --no-sync)    DO_SYNC="no"; shift ;;
        --help|-h)    usage; exit 0 ;;
        *) log_error "Unknown option: $1"; usage; exit 1 ;;
    esac
done

case "$SCOPE" in personal|team|both) ;; *)
    log_error "--scope must be personal, team or both (got: $SCOPE)"; exit 1 ;;
esac

# Where the personal-scope config lives. KIROCREW_SYNC_CONFIG is honoured for
# the same reason kirocrew-sync.sh honours it: tests and multi-config setups
# point it elsewhere. The team config sits beside it as <name>-team.sh.
CONFIG_FILE="${KIROCREW_SYNC_CONFIG:-$SCRIPT_DIR/config.sh}"
TEAM_CONFIG_FILE="${CONFIG_FILE%.sh}-team.sh"

echo "KiroCrew Sync onboarding (R2 / S3-compatible)"
echo "============================================="

# --- 1. prerequisites -------------------------------------------------------

MISSING=0
for tool in git python3 aws; do
    if ! command -v "$tool" > /dev/null 2>&1; then
        log_error "Missing prerequisite: $tool"
        MISSING=1
    fi
done
if [ "$MISSING" -eq 1 ]; then
    log_info "macOS:  brew install git python3 awscli"
    log_info "Linux:  apt/dnf install git python3, then: pip install awscli"
    exit 1
fi
log_success "Prerequisites present (git, python3, aws)"

# wrangler is optional - it only powers discovery. Bare install first, npx as
# the fallback so a node-only machine still gets discovery without a global
# install.
WRANGLER=""
if command -v wrangler > /dev/null 2>&1; then
    WRANGLER="wrangler"
elif command -v npx > /dev/null 2>&1; then
    WRANGLER="npx -y wrangler"
fi

# --- 2. account id -> endpoint ----------------------------------------------

if [ -z "$ENDPOINT" ]; then
    if [ -z "$ACCOUNT_ID" ] && [ -n "$WRANGLER" ]; then
        log_info "Discovering your Cloudflare account via wrangler..."
        if ! $WRANGLER whoami > /dev/null 2>&1; then
            log_info "Not logged in - opening the OAuth flow (one browser click)"
            $WRANGLER login
        fi
        # The account id is the only 32-hex token in whoami's output table.
        ACCOUNT_ID="$($WRANGLER whoami 2> /dev/null \
            | grep -oE '[0-9a-f]{32}' | head -1 || true)"
    fi
    if [ -z "$ACCOUNT_ID" ] && [ -t 0 ]; then
        read -r -p "Cloudflare account ID (32 hex chars, dashboard -> R2 overview): " ACCOUNT_ID
    fi
    if [ -z "$ACCOUNT_ID" ]; then
        log_error "No account id: pass --account-id (or --endpoint), or install wrangler"
        exit 1
    fi
    ENDPOINT="https://${ACCOUNT_ID}.r2.cloudflarestorage.com"
fi
log_success "Endpoint: $ENDPOINT"

# Soft bucket check - discovery only, the probe below is the real gate.
if [ -n "$WRANGLER" ]; then
    if $WRANGLER r2 bucket list 2> /dev/null | grep -q "$BUCKET"; then
        log_success "Bucket exists: $BUCKET"
    else
        log_warn "Bucket '$BUCKET' not seen via wrangler - if it is missing:"
        log_info "  $WRANGLER r2 bucket create $BUCKET"
    fi
fi

# --- 3. config file(s) ------------------------------------------------------

# Only rewritten when the content would actually change, and the old file is
# kept beside it - config.sh is gitignored, so a backup is the only history
# it gets.
write_config() {
    local path="$1" scope="$2" prefix="$3" tmp
    tmp="$(mktemp)"
    cat > "$tmp" << EOF
#!/usr/bin/env bash
# KiroCrew Sync configuration - ${scope} scope, S3-compatible backend (R2).
# Generated by onboard.sh; safe to edit, re-running onboard keeps your edits
# backed up beside it. Credentials are NOT here - they live in the
# '${PROFILE}' aws CLI profile.

export SYNC_BACKEND="s3"
export KIROCREW_DIR="\$HOME/.kiro/crew"
export SYNC_SCOPE="${scope}"
export SYNC_PORTABLE_PATHS=1
export KIROCREW_PATH_MAP="\$KIROCREW_DIR/path_map.conf"

# S3_PREFIX MUST stay distinct per scope; it is the only thing keeping one
# scope's published bundle from overwriting the other's in this bucket.
export S3_BUCKET="${BUCKET}"
export S3_PREFIX="${prefix}"
export AWS_PROFILE="${PROFILE}"
export S3_ENDPOINT_URL="${ENDPOINT}"
export S3_REGION="${REGION}"
EOF
    if [ -f "$path" ] && cmp -s "$tmp" "$path"; then
        log_success "Config already current: $path"
        rm -f "$tmp"
        return 0
    fi
    if [ -f "$path" ]; then
        cp "$path" "$path.bak.$(date +%Y%m%d%H%M%S)"
        log_warn "Existing $path backed up beside it"
    fi
    mv "$tmp" "$path"
    log_success "Wrote $path (scope: $scope, prefix: $prefix)"
}

case "$SCOPE" in
    personal) write_config "$CONFIG_FILE" personal sync/personal ;;
    team)     write_config "$TEAM_CONFIG_FILE" team sync/team ;;
    both)     write_config "$CONFIG_FILE" personal sync/personal
              write_config "$TEAM_CONFIG_FILE" team sync/team ;;
esac

# --- 4. credentials ---------------------------------------------------------

# The probe is argument-for-argument the one backends/s3.sh runs before every
# operation, so passing here means the backend will work.
probe() {
    aws s3 ls "s3://$BUCKET" --profile "$PROFILE" \
        --endpoint-url "$ENDPOINT" --region "$REGION" > /dev/null 2>&1
}

if probe; then
    log_success "Credential profile '$PROFILE' already works - keeping it"
else
    log_info "No working credentials in profile '$PROFILE' - minting an R2 token"
    log_info ""
    log_info "This is the one step Cloudflare only allows in the dashboard"
    log_info "(no OAuth/CLI path exists for R2 S3 keys). On the page:"
    log_info "  1. Create Account API token"
    log_info "  2. Permissions: Object Read & Write, scoped to bucket '$BUCKET'"
    log_info "  3. Suggested name: kirocrew-sync-$(hostname -s 2>/dev/null || echo device)"
    log_info "  4. Copy the Access Key ID and Secret Access Key it shows"
    TOKEN_URL="https://dash.cloudflare.com/${ACCOUNT_ID:-?}/r2/api-tokens"
    if [ -t 0 ]; then
        case "$(uname -s)" in
            Darwin) open "$TOKEN_URL" 2> /dev/null || true ;;
            Linux)  xdg-open "$TOKEN_URL" 2> /dev/null || true ;;
        esac
    fi
    log_info "Token page: $TOKEN_URL"
    echo
    read -r -p "Access Key ID: " KEY_ID
    read -r -s -p "Secret Access Key (hidden): " KEY_SECRET
    echo
    if [ -z "$KEY_ID" ] || [ -z "$KEY_SECRET" ]; then
        log_error "Empty credential - aborting without writing anything"
        exit 1
    fi
    # aws configure set creates ~/.aws files with sane permissions itself;
    # the secret goes through no echo, no argv longer than this line, no file
    # but the CLI's own.
    aws configure set aws_access_key_id "$KEY_ID" --profile "$PROFILE"
    aws configure set aws_secret_access_key "$KEY_SECRET" --profile "$PROFILE"
    aws configure set region "$REGION" --profile "$PROFILE"
    unset KEY_SECRET
    if probe; then
        log_success "Credentials verified against $BUCKET"
    else
        log_error "Probe still failing. Check, in this order:"
        log_info "  aws s3 ls s3://$BUCKET --profile $PROFILE --endpoint-url $ENDPOINT --region $REGION"
        log_info "  - signature/region error: region must be '$REGION' (R2: auto)"
        log_info "  - DNS error: endpoint is the account host, no bucket appended"
        log_info "  - 403: token not scoped to '$BUCKET', or not Object Read & Write"
        log_info "See docs/backends/s3.md"
        exit 1
    fi
fi

# --- 5. init + doctor + status ----------------------------------------------

KIROCREW_SYNC_CONFIG="$CONFIG_FILE" "$SCRIPT_DIR/kirocrew-sync.sh" init
# Informational from here on - a fresh machine with no KiroCrew data yet
# should still finish onboarding.
KIROCREW_SYNC_CONFIG="$CONFIG_FILE" "$SCRIPT_DIR/kirocrew-sync.sh" doctor || \
    log_warn "doctor reported issues above - fix them before the first sync"
KIROCREW_SYNC_CONFIG="$CONFIG_FILE" "$SCRIPT_DIR/kirocrew-sync.sh" status || true

# --- 6. first sync ----------------------------------------------------------

if [ "$DO_SYNC" = "ask" ]; then
    if [ -t 0 ]; then
        read -r -p "Run the first sync now? [y/N] " ANSWER
        case "$ANSWER" in y|Y|yes) DO_SYNC="yes" ;; *) DO_SYNC="no" ;; esac
    else
        DO_SYNC="no"
    fi
fi
if [ "$DO_SYNC" = "yes" ]; then
    KIROCREW_SYNC_CONFIG="$CONFIG_FILE" "$SCRIPT_DIR/kirocrew-sync.sh" sync
else
    log_info "Skipping first sync. When ready: ./kirocrew-sync.sh sync"
fi

echo
log_success "Onboarding complete"
log_info "Next steps:"
log_info "  - background sync:  docs/daemon.md (launchd on macOS, systemd on Linux)"
log_info "  - seed from another machine instead of merging from zero:"
log_info "      there:  ./kirocrew-sync.sh export -o snap.tar.gz"
log_info "      here:   ./kirocrew-sync.sh import snap.tar.gz"
if [ "$SCOPE" != "personal" ]; then
    log_info "  - team scope runs with: KIROCREW_SYNC_CONFIG=$TEAM_CONFIG_FILE ./kirocrew-sync.sh sync --team"
fi
