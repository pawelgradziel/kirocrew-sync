#!/usr/bin/env bash
#
# Settings can come from config.sh or from the environment, and the two have to
# agree on who wins: an explicit environment variable overrides config.sh, and
# config.sh overrides the built-in default.
#
# The script's prologue is sourced with `help` (which touches nothing) so the
# resolved values can be read back directly, without needing rclone or the AWS
# CLI to be installed on the machine running the tests.
#
# Run: ./tests/test_config_precedence.sh
#

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0

assert_eq() {
    local name="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        printf '  \033[0;32m✓\033[0m %s\n' "$name"
        PASS=$((PASS + 1))
    else
        printf '  \033[0;31m✗\033[0m %s\n      expected: %s\n      actual:   %s\n' \
            "$name" "$expected" "$actual"
        FAIL=$((FAIL + 1))
    fi
}

echo "kirocrew-sync.sh setting precedence"
echo

# --- a copy of the tool, so the test never writes to the real config --------
TOOL="$WORK/tool"
cp -r "$REPO_DIR" "$TOOL"
rm -rf "$TOOL/.git"

CONFIG_DIR="$WORK/crew-from-config"
ENV_DIR="$WORK/crew-from-env"

cat > "$TOOL/config.sh" <<EOF
#!/usr/bin/env bash
# test config -- deliberately different from every built-in default
export SYNC_BACKEND="s3"
export KIROCREW_DIR="$CONFIG_DIR"
export SYNC_PORTABLE_PATHS=0
export KIROCREW_PATH_MAP="$WORK/from-config.conf"
EOF

# Resolved value of one variable, under a given environment.
resolve() {
    local var="$1"
    shift
    env "$@" bash -c '
        source "$1" help > /dev/null 2>&1
        printf "%s" "${!2}"
    ' _ "$TOOL/kirocrew-sync.sh" "$var"
}

# The environment of a plain run, with nothing preset.
CLEAN=(-u SYNC_BACKEND -u KIROCREW_DIR -u SYNC_PORTABLE_PATHS -u KIROCREW_PATH_MAP)

# --- config.sh is honoured when the environment says nothing ----------------
assert_eq "config.sh chooses the backend" \
    "s3" "$(resolve BACKEND "${CLEAN[@]}")"
assert_eq "config.sh chooses the data directory" \
    "$CONFIG_DIR" "$(resolve KIROCREW_DIR "${CLEAN[@]}")"
assert_eq "config.sh chooses path portability" \
    "0" "$(resolve SYNC_PORTABLE_PATHS "${CLEAN[@]}")"
assert_eq "config.sh chooses the path map" \
    "$WORK/from-config.conf" "$(resolve KIROCREW_PATH_MAP "${CLEAN[@]}")"

# --- an explicit environment variable still wins ----------------------------
assert_eq "SYNC_BACKEND overrides config.sh" \
    "rsync" "$(resolve BACKEND SYNC_BACKEND=rsync)"
assert_eq "KIROCREW_DIR overrides config.sh" \
    "$ENV_DIR" "$(resolve KIROCREW_DIR KIROCREW_DIR="$ENV_DIR")"
assert_eq "SYNC_PORTABLE_PATHS overrides config.sh" \
    "1" "$(resolve SYNC_PORTABLE_PATHS SYNC_PORTABLE_PATHS=1)"
assert_eq "KIROCREW_PATH_MAP overrides config.sh" \
    "$WORK/from-env.conf" "$(resolve KIROCREW_PATH_MAP KIROCREW_PATH_MAP="$WORK/from-env.conf")"

# --- the built-in defaults still apply when nothing sets them ---------------
printf '#!/usr/bin/env bash\n# test config -- sets nothing\n' > "$TOOL/config.sh"

assert_eq "the default backend is gdrive" \
    "gdrive" "$(resolve BACKEND "${CLEAN[@]}")"
assert_eq "the default data directory is under \$HOME" \
    "$WORK/home/.kiro/crew" "$(resolve KIROCREW_DIR "${CLEAN[@]}" HOME="$WORK/home")"
assert_eq "path portability defaults to on" \
    "1" "$(resolve SYNC_PORTABLE_PATHS "${CLEAN[@]}")"
assert_eq "the path map defaults to the data directory" \
    "$ENV_DIR/path_map.conf" \
    "$(resolve KIROCREW_PATH_MAP "${CLEAN[@]}" KIROCREW_DIR="$ENV_DIR")"

echo
if [ "$FAIL" -gt 0 ]; then
    printf '\033[0;31m✗\033[0m %d passed, %d failed\n' "$PASS" "$FAIL"
    exit 1
fi
printf '\033[0;32m✓\033[0m %d passed\n' "$PASS"
