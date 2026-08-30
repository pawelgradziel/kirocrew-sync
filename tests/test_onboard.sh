#!/usr/bin/env bash
#
# onboard.sh turns the manual R2 setup (hand-edited config.sh, dashboard
# token mint, aws configure, S3_REGION=auto) into one run. What this suite
# pins down:
#
#   - the config it writes is the one backends/s3.sh needs: s3 backend,
#     endpoint derived from the account id, region auto, and the fixed
#     per-scope prefixes (sync/personal / sync/team) that match the
#     container peer's reference pair;
#   - credentials go through `aws configure set` under the right profile,
#     with region auto - and ONLY when the probe fails; a machine whose
#     profile already works is left alone (no configure calls at all);
#   - the account id is parsed out of `wrangler whoami`'s table when no
#     --account-id is given;
#   - re-running is safe: identical content -> no rewrite and no backup;
#     changed content -> old config backed up beside it.
#
# Everything runs against stub `aws` and `wrangler` binaries placed earlier
# on PATH, a throwaway HOME, and KIROCREW_SYNC_CONFIG pointing into the work
# dir - no credentials, no network, no real ~/.aws or ~/.kiro touched. As
# with tests/test_backend_s3_endpoint.sh, this proves the command lines and
# files are right, not that R2 accepts them (GOAL.md: never run end-to-end
# against a real remote backend).
#
# Run: ./tests/test_onboard.sh
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

assert_contains() {
    local name="$1" haystack="$2" needle="$3"
    if grep -aqF -- "$needle" <<< "$haystack"; then ok "$name"
    else bad "$name" "expected to find: $needle"; fi
}
assert_not_contains() {
    local name="$1" haystack="$2" needle="$3"
    if grep -aqF -- "$needle" <<< "$haystack"; then
        bad "$name" "should not contain: $needle"
    else ok "$name"; fi
}
assert_file() {
    local name="$1" path="$2"
    if [ -f "$path" ]; then ok "$name"; else bad "$name" "missing: $path"; fi
}

# --- stubs ------------------------------------------------------------------

export AWS_LOG="$WORK/aws_calls.log"
export WR_LOG="$WORK/wrangler_calls.log"
export PROBE_OK="$WORK/probe_ok"

mkdir -p "$WORK/bin" "$WORK/home"

cat > "$WORK/bin/aws" << 'EOF'
#!/usr/bin/env bash
echo "aws $*" >> "$AWS_LOG"
case "$1" in
    s3)
        # The reachability probe: fails until credentials have been "stored".
        if [ -f "$PROBE_OK" ]; then exit 0; fi
        echo "An error occurred (403) when calling the ListObjectsV2 operation" >&2
        exit 1
        ;;
    configure)
        # Storing the secret is what makes the probe start succeeding.
        if [ "$2" = "set" ] && [ "$3" = "aws_secret_access_key" ]; then
            touch "$PROBE_OK"
        fi
        exit 0
        ;;
esac
exit 0
EOF

cat > "$WORK/bin/wrangler" << 'EOF'
#!/usr/bin/env bash
echo "wrangler $*" >> "$WR_LOG"
case "$1" in
    whoami)
        echo '│ Account Name │ Account ID                       │'
        echo '│ test account │ deadbeefdeadbeefdeadbeefdeadbeef │'
        ;;
    r2)
        echo 'name:           kirocrew-state'
        ;;
esac
exit 0
EOF
chmod +x "$WORK/bin/aws" "$WORK/bin/wrangler"

run_onboard() {
    PATH="$WORK/bin:$PATH" HOME="$WORK/home" \
    KIROCREW_SYNC_CONFIG="$WORK/config.sh" \
        "$REPO_DIR/onboard.sh" "$@" 2>&1
}

# --------------------------------------------------------------------------

echo "== fresh machine: wrangler discovery, mint, config, no sync =="
OUT="$(printf 'AKIDTEST\nSECRETTEST\n' | run_onboard --no-sync)"
RC=$?

assert_contains "onboarding completed" "$OUT" "Onboarding complete"
[ "$RC" -eq 0 ] && ok "exit 0" || bad "exit 0" "rc=$RC" "$OUT"

CFG="$(cat "$WORK/config.sh" 2>/dev/null)"
assert_contains "backend is s3"                "$CFG" 'SYNC_BACKEND="s3"'
assert_contains "endpoint derived from whoami" "$CFG" 'https://deadbeefdeadbeefdeadbeefdeadbeef.r2.cloudflarestorage.com'
assert_contains "region is auto"               "$CFG" 'S3_REGION="auto"'
assert_contains "personal prefix"              "$CFG" 'S3_PREFIX="sync/personal"'
assert_contains "default bucket"               "$CFG" 'S3_BUCKET="kirocrew-state"'
assert_contains "profile r2"                   "$CFG" 'AWS_PROFILE="r2"'

AWS_CALLS="$(cat "$AWS_LOG")"
assert_contains "key id stored under profile"  "$AWS_CALLS" "configure set aws_access_key_id AKIDTEST --profile r2"
assert_contains "secret stored under profile"  "$AWS_CALLS" "configure set aws_secret_access_key SECRETTEST --profile r2"
assert_contains "region stored under profile"  "$AWS_CALLS" "configure set region auto --profile r2"
assert_contains "probe carries endpoint"       "$AWS_CALLS" "--endpoint-url https://deadbeefdeadbeefdeadbeefdeadbeef.r2.cloudflarestorage.com"
assert_contains "probe carries region"         "$AWS_CALLS" "--region auto"

echo
echo "== re-run: working credentials kept, identical config not rewritten =="
: > "$AWS_LOG"
OUT="$(run_onboard --no-sync < /dev/null)"

assert_contains     "existing profile detected"  "$OUT" "already works"
assert_not_contains "no re-mint on re-run"       "$(cat "$AWS_LOG")" "configure set"
assert_contains     "config detected as current" "$OUT" "already current"
BAKS="$(ls "$WORK"/config.sh.bak.* 2>/dev/null | wc -l | tr -d ' ')"
[ "$BAKS" = "0" ] && ok "no backup for identical content" || bad "no backup for identical content" "found $BAKS"

echo
echo "== changed config is backed up, not clobbered =="
echo "# user edit" >> "$WORK/config.sh"
OUT="$(run_onboard --no-sync < /dev/null)"
BAKS="$(ls "$WORK"/config.sh.bak.* 2>/dev/null | wc -l | tr -d ' ')"
[ "$BAKS" = "1" ] && ok "backup created on change" || bad "backup created on change" "found $BAKS"
assert_contains "backup announced" "$OUT" "backed up"
assert_not_contains "regenerated config drops nothing silently" "$(cat "$WORK/config.sh")" "# user edit"

echo
echo "== --scope team with explicit --account-id (no wrangler needed) =="
OUT="$(PATH="$WORK/bin:$PATH" HOME="$WORK/home" \
    KIROCREW_SYNC_CONFIG="$WORK/scoped.sh" \
    "$REPO_DIR/onboard.sh" --scope team --no-sync \
        --account-id cafecafecafecafecafecafecafecafe < /dev/null 2>&1)"

TCFG="$(cat "$WORK/scoped-team.sh" 2>/dev/null)"
assert_file     "team config written beside personal path" "$WORK/scoped-team.sh"
assert_contains "team scope"    "$TCFG" 'SYNC_SCOPE="team"'
assert_contains "team prefix"   "$TCFG" 'S3_PREFIX="sync/team"'
assert_contains "explicit account id used" "$TCFG" 'https://cafecafecafecafecafecafecafecafe.r2.cloudflarestorage.com'
assert_contains "team invocation hint printed" "$OUT" "sync --team"

# --------------------------------------------------------------------------

echo
echo "-----------------------------------------"
printf '  \033[0;32m%d passed\033[0m, \033[0;31m%d failed\033[0m\n' "$PASS" "$FAIL"
echo "-----------------------------------------"
[ "$FAIL" -eq 0 ]
