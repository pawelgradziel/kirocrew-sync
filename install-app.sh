#!/usr/bin/env bash
#
# Install (or uninstall) the KiroCrew Sync app into KiroCrew.
#
# Usage:
#   ./install-app.sh              Install/update the app
#   ./install-app.sh --uninstall  Remove the installed app
#   ./install-app.sh --help       Show this help
#
# Two install paths, tried in this order:
#
#   1. Gateway API (preferred). If a KiroCrew gateway is reachable on
#      localhost, the app is installed through its real install transaction
#      (POST /api/apps/install) — the same thing the dashboard's "Install
#      from Path" does. This is the only path that registers agents/skills/
#      crons and starts the app's backend, and it is the only way the app
#      ever gets its per-app secret (.app_secret), which its backend
#      requires to reach the KiroCrew notification API.
#
#   2. Offline staging (fallback). If no gateway is reachable, the script
#      copies the app's files itself and writes a complete, schema-correct
#      installed.json (including .app_secret) so the app is ready to be
#      picked up the moment you Enable it from a running gateway's
#      dashboard. It does NOT register resources or start a backend —
#      only a live gateway can do that.
#
# Either way, the app is installed but NOT enabled, and third-party app
# execution is denied by default: you still need to trust it and enable it
# from the dashboard afterward. See app/README.md for details.
#
# Safe to re-run: each pass copies fresh files over the existing install
# directory, re-initializes the (non-destructive) offline database schema,
# and refreshes installation metadata without touching sync history or an
# already-generated app secret.

set -euo pipefail

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# All to stderr, not just log_error: several functions below (e.g.
# mint_gateway_token) are called via command substitution — token="$(...)"
# — to capture a return value on stdout. A log_* call that wrote to stdout
# would get silently captured into that value instead of ever reaching the
# terminal, swallowing exactly the diagnostic a failure needs to explain
# itself.
log_info() { echo -e "${BLUE}ℹ${NC} $*" >&2; }
log_success() { echo -e "${GREEN}✓${NC} $*" >&2; }
log_warn() { echo -e "${YELLOW}⚠${NC} $*" >&2; }
log_error() { echo -e "${RED}✗${NC} $*" >&2; }

# Paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SRC_DIR="$SCRIPT_DIR/app"
KIROCREW_HOME="${KIROCREW_HOME:-$HOME/.kiro/crew}"
SYNC_DIR="$KIROCREW_HOME/workspace/kirocrew-sync"
LOCAL_SECRET_FILE="$KIROCREW_HOME/.local_secret"

# Gateway HTTP location. KIROCREW_PORT mirrors the env var the gateway
# itself honors (kiro_crew.config.loader.DASHBOARD_PORT), so a non-default
# port setup is picked up automatically.
GATEWAY_PORT="${KIROCREW_PORT:-5476}"
GATEWAY_BASE="http://127.0.0.1:${GATEWAY_PORT}"

# The app's own identity, read from its manifest — every installed.json
# this script writes, and every gateway API call it makes, keys off this
# rather than a hardcoded guess. Falls back to the literal "kirocrew-sync"
# (this repo's fixed app name) so --help/--uninstall still work even
# without python3 or a readable app.json; do_install's check_prerequisites
# re-derives it strictly and aborts if it can't.
_read_manifest_field() {
    python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
    print(data.get(sys.argv[2], "") or "")
except Exception:
    print("")
' "$APP_SRC_DIR/app.json" "$1" 2>/dev/null
}

APP_NAME="kirocrew-sync"
APP_VERSION=""
APP_DISPLAY_NAME="kirocrew-sync"
if command -v python3 >/dev/null 2>&1 && [ -f "$APP_SRC_DIR/app.json" ]; then
    _name="$(_read_manifest_field name)"
    [ -n "$_name" ] && APP_NAME="$_name"
    APP_VERSION="$(_read_manifest_field version)"
    _display="$(_read_manifest_field displayName)"
    [ -n "$_display" ] && APP_DISPLAY_NAME="$_display"
fi
APP_DEST_DIR="$KIROCREW_HOME/apps/$APP_NAME"

usage() {
    cat <<EOF
Usage: $(basename "${BASH_SOURCE[0]}") [--uninstall|--help]

  (no args)     Install or update the app at $APP_DEST_DIR
                Prefers a reachable KiroCrew gateway's install API; falls
                back to staging files + metadata offline otherwise.
  --uninstall   Remove the app (does not touch $SYNC_DIR)
  --help        Show this help

Env overrides: KIROCREW_HOME (default ~/.kiro/crew), KIROCREW_PORT (default
5476) — both match the names KiroCrew itself honors.
EOF
}

# Verify everything the install needs is present *before* copying anything,
# so a missing prerequisite fails loudly instead of leaving a half-installed
# app directory behind. Also re-derives APP_NAME/APP_VERSION/APP_DISPLAY_NAME
# strictly, now that python3 + app.json are both confirmed present, and
# aborts if the manifest has no usable name — nothing below this point may
# ever write an installed.json (or call the gateway) without a real name.
check_prerequisites() {
    local missing=0

    if [ ! -d "$SYNC_DIR" ]; then
        log_error "kirocrew-sync not found at $SYNC_DIR"
        echo
        echo "Please install kirocrew-sync first:"
        echo "  cd ~/.kiro/crew/workspace"
        echo "  git clone https://github.com/pawelgradziel/kirocrew-sync.git"
        echo "  cd kirocrew-sync"
        echo "  ./kirocrew-sync.sh init"
        missing=1
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        log_error "python3 is required but was not found on PATH"
        missing=1
    fi

    for required in "$APP_SRC_DIR/app.json" "$APP_SRC_DIR/backend" "$APP_SRC_DIR/ui"; do
        if [ ! -e "$required" ]; then
            log_error "Expected app source missing: $required"
            log_error "Your kirocrew-sync checkout looks incomplete or corrupted."
            missing=1
        fi
    done

    if [ "$missing" -ne 0 ]; then
        echo
        log_error "Prerequisite check failed, aborting before copying any files."
        exit 1
    fi

    APP_NAME="$(_read_manifest_field name)"
    if [ -z "$APP_NAME" ]; then
        log_error "app.json has no usable \"name\" field: $APP_SRC_DIR/app.json"
        exit 1
    fi
    APP_VERSION="$(_read_manifest_field version)"
    [ -z "$APP_VERSION" ] && APP_VERSION="0.0.0"
    APP_DISPLAY_NAME="$(_read_manifest_field displayName)"
    [ -z "$APP_DISPLAY_NAME" ] && APP_DISPLAY_NAME="$APP_NAME"
    APP_DEST_DIR="$KIROCREW_HOME/apps/$APP_NAME"
}

# ---------------------------------------------------------------------------
# Gateway API helpers
# ---------------------------------------------------------------------------

gateway_reachable() {
    command -v curl >/dev/null 2>&1 || return 1
    # /api/health is an explicit unauthenticated liveness bypass in
    # KiroCrew's token_auth middleware — safe to probe with no credentials.
    curl -fsS --max-time 2 "$GATEWAY_BASE/api/health" >/dev/null 2>&1
}

# Mints a short-lived dashboard token the same way KiroCrew's own local
# tooling does (see skills/self-nudge-loop/scaffold.sh in the kirocrew
# source tree): GET /api/token/local from loopback with the per-install
# secret at ~/.kiro/crew/.local_secret in an X-Local-Secret header. That
# secret is readable only by this user, exactly like any other file under
# $KIROCREW_HOME — reading it to talk to our own local gateway is the
# supported bootstrap, not a credential we're inventing a use for. Prints
# the token to stdout on success; returns 1 with a log_warn reason
# otherwise (nothing is printed to stdout on failure).
mint_gateway_token() {
    if [ ! -r "$LOCAL_SECRET_FILE" ]; then
        log_warn "Cannot read $LOCAL_SECRET_FILE — cannot authenticate to the gateway automatically."
        return 1
    fi
    local secret token_json token
    secret="$(cat "$LOCAL_SECRET_FILE")"
    if ! token_json="$(curl -fsS --max-time 5 -H "X-Local-Secret: $secret" \
        "$GATEWAY_BASE/api/token/local?ttl=5m" 2>/dev/null)"; then
        log_warn "Gateway rejected the local token request."
        return 1
    fi
    token="$(python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("token", ""))
except Exception:
    pass
' <<<"$token_json")"
    if [ -z "$token" ]; then
        log_warn "Gateway did not return a token."
        return 1
    fi
    printf '%s' "$token"
}

_json_source_body() {
    python3 -c 'import json,sys; print(json.dumps({"source": sys.argv[1]}))' "$APP_SRC_DIR"
}

# Installs (or, if already installed, updates) the app through the real
# gateway install transaction. Returns 0 on success.
install_via_gateway() {
    local token resp http_status http_body error

    token="$(mint_gateway_token)" || return 1
    [ -z "$token" ] && return 1

    resp="$(curl -sS --max-time 60 -w '\n%{http_code}' -X POST \
        -H 'Content-Type: application/json' \
        -d "$(_json_source_body)" \
        "$GATEWAY_BASE/api/apps/install?token=$token")"
    http_status="${resp##*$'\n'}"
    http_body="${resp%$'\n'*}"

    if [ "$http_status" = "201" ]; then
        log_success "Installed via the KiroCrew gateway (POST /api/apps/install)"
        return 0
    fi

    error="$(python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("error", ""))
except Exception:
    print("")
' <<<"$http_body" 2>/dev/null || true)"

    if printf '%s' "$error" | grep -qi "already installed"; then
        log_info "Already installed via the gateway — updating in place (POST /api/apps/$APP_NAME/update)"
        resp="$(curl -sS --max-time 60 -w '\n%{http_code}' -X POST \
            -H 'Content-Type: application/json' \
            -d "$(_json_source_body)" \
            "$GATEWAY_BASE/api/apps/$APP_NAME/update?token=$token")"
        http_status="${resp##*$'\n'}"
        http_body="${resp%$'\n'*}"
        if [ "$http_status" = "200" ]; then
            log_success "Updated via the KiroCrew gateway (POST /api/apps/$APP_NAME/update)"
            return 0
        fi
        log_warn "Gateway update failed (HTTP $http_status): $(printf '%s' "$http_body" | head -c 300)"
        return 1
    fi

    log_warn "Gateway install failed (HTTP $http_status): ${error:-$(printf '%s' "$http_body" | head -c 300)}"
    return 1
}

print_manual_install_instructions() {
    echo
    log_info "To install manually instead:"
    echo "  1. Open the KiroCrew dashboard → Apps"
    echo "  2. Click the sources icon (top-right of the Apps page — \"Manage app sources\")"
    echo "  3. Under \"Install from Path\", enter:"
    echo "       $APP_SRC_DIR"
    echo "  4. Click Install"
}

print_trust_and_enable_steps() {
    echo "Next steps (required before it does anything):"
    echo "  1. Settings → Security → Third-party apps → trust \"$APP_NAME\""
    echo "     (or turn on \"Allow all third-party apps\") — third-party app"
    echo "     execution is denied by default, gateway or not."
    echo "  2. Apps → $APP_DISPLAY_NAME → Enable"
    echo "     (this is what registers its agents/skills/crons and starts its backend)"
    echo "  3. Navigate to /apps/$APP_NAME in the dashboard"
}

# ---------------------------------------------------------------------------
# Backend virtualenv
# ---------------------------------------------------------------------------
#
# The gateway spawns an ASGI-type app backend (backend.type: "asgi" in
# app.json) with:
#     <app dir>/.venv/bin/python3   — if that file exists
#     else the gateway's own bundled interpreter
# The gateway's bundled interpreter does NOT have fastapi/uvicorn/pydantic
# installed (it's the gateway's own runtime, not ours), so without a venv
# here the backend dies at import time. This MUST run against the INSTALLED
# app directory ($APP_DEST_DIR), after files are already in place there —
# never against $APP_SRC_DIR, since a machine-specific venv must not live in
# (or be copied out of) the repo's source tree.
#
# Called after BOTH install paths: the offline path copies files itself, and
# the gateway-API path has the gateway do the copy, so either way this must
# run only once the destination directory is known to exist and be current.
provision_venv() {
    local dest="$1"
    local req="$dest/requirements.txt"
    local venv_dir="$dest/.venv"

    if [ ! -f "$req" ]; then
        log_warn "No requirements.txt at $req — skipping backend venv setup."
        log_warn "The backend needs fastapi/uvicorn/pydantic; without them it will not start."
        return 1
    fi

    if [ -x "$venv_dir/bin/python3" ] \
        && "$venv_dir/bin/python3" -c 'import fastapi, uvicorn, pydantic' >/dev/null 2>&1; then
        log_info "Backend venv already present with required packages — leaving it as is."
        return 0
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        log_error "python3 not found — cannot create the backend venv."
        return 1
    fi

    log_info "Creating backend virtualenv at $venv_dir..."
    rm -rf "$venv_dir"
    if ! python3 -m venv "$venv_dir" >/dev/null 2>&1; then
        log_error "Failed to create the backend virtualenv (the python3-venv package may"
        log_error "not be installed)."
        rm -rf "$venv_dir"
        log_error "The backend will fall back to the gateway's own interpreter, which does"
        log_error "NOT have fastapi/uvicorn/pydantic and will fail to start. Install the venv"
        log_error "module (e.g. 'apt install python3-venv' / 'dnf install python3-venv') and"
        log_error "re-run ./install-app.sh, or run manually:"
        log_error "  python3 -m venv $venv_dir && $venv_dir/bin/python3 -m pip install -r $req"
        return 1
    fi

    log_info "Installing backend dependencies (fastapi, uvicorn, pydantic)..."
    local pip_log
    pip_log="$(mktemp)"
    if ! "$venv_dir/bin/python3" -m pip install --quiet --disable-pip-version-check \
        -r "$req" >"$pip_log" 2>&1; then
        log_error "Failed to install backend dependencies into $venv_dir:"
        tail -n 20 "$pip_log" >&2
        rm -f "$pip_log"
        rm -rf "$venv_dir"
        log_error "Removed the incomplete venv rather than leaving it behind — a half-built"
        log_error "venv would be preferred over the gateway's own interpreter and the backend"
        log_error "would fail with confusing missing-package errors instead of falling back"
        log_error "cleanly. Check your network connection and re-run ./install-app.sh, or run"
        log_error "manually:"
        log_error "  python3 -m venv $venv_dir && $venv_dir/bin/python3 -m pip install -r $req"
        return 1
    fi
    rm -f "$pip_log"

    log_success "Backend virtualenv ready at $venv_dir"
    return 0
}

# ---------------------------------------------------------------------------
# Offline fallback: stage files + write correct installed.json ourselves
# ---------------------------------------------------------------------------

do_offline_install() {
    log_info "Creating app directory..."
    mkdir -p "$APP_DEST_DIR"/{backend,data,ui}

    log_info "Copying app files..."
    cp "$APP_SRC_DIR/app.json" "$APP_DEST_DIR/"
    cp -r "$APP_SRC_DIR/backend"/. "$APP_DEST_DIR/backend/"
    cp -r "$APP_SRC_DIR/ui"/. "$APP_DEST_DIR/ui/"
    if [ -f "$APP_SRC_DIR/requirements.txt" ]; then
        cp "$APP_SRC_DIR/requirements.txt" "$APP_DEST_DIR/"
    fi
    find "$APP_DEST_DIR/backend" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    log_success "App files copied"

    # The backend's own managers call Database.initialize() on construction
    # (CREATE TABLE IF NOT EXISTS ...), so this only pre-creates the schema
    # for inspection before the app is ever enabled; it's not required for
    # correctness and is always safe to re-run.
    log_info "Initializing database..."
    python3 "$APP_DEST_DIR/backend/database.py"

    log_info "Recording installation metadata..."
    local now installed_at existing
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    installed_at="$now"
    if [ -f "$APP_DEST_DIR/installed.json" ]; then
        existing="$(python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(json.load(f).get("installedAt", ""))
except Exception:
    print("")
' "$APP_DEST_DIR/installed.json" 2>/dev/null || true)"
        [ -n "$existing" ] && installed_at="$existing"
    fi

    # Schema per kiro_crew.apps.manager.InstalledApp (installed.json's
    # authoritative field list). "name" is the fix this whole rework exists
    # for: KiroCrew's boot-time resource reconcile keys trust and admission
    # off this field, and a missing/empty name is read back as "" — which
    # can never appear in agent.apps_trusted, so the app's resources get
    # silently revoked as an untrusted, unnamed app. origin="local" matches
    # the field's own documented meaning ("installed from a local directory
    # path"); resources/lifecycle="gateway" so the dashboard's own
    # Enable/Update/Uninstall controls manage it exactly like a real
    # gateway-driven install; schemaVersion=2 is current. enabled is always
    # false here — matching the gateway's own install_app(), which never
    # auto-enables — so nothing runs until you explicitly trust + enable it.
    python3 - "$APP_DEST_DIR/installed.json" "$APP_NAME" "$APP_VERSION" \
        "$APP_DISPLAY_NAME" "$installed_at" "$now" "$APP_SRC_DIR" <<'PYEOF'
import json, sys

path, name, version, display_name, installed_at, updated_at, source = sys.argv[1:8]
meta = {
    "name": name,
    "version": version,
    "displayName": display_name,
    "enabled": False,
    "installedAt": installed_at,
    "updatedAt": updated_at,
    "source": source,
    "origin": "local",
    "resources": "gateway",
    "lifecycle": "gateway",
    "schemaVersion": 2,
    "dev": False,
}
with open(path, "w") as f:
    json.dump(meta, f, indent=2)
    f.write("\n")
PYEOF
    log_success "Installation metadata recorded (name=$APP_NAME)"

    # The gateway's install transaction always generates .app_secret
    # alongside installed.json (kiro_crew.apps.manager.install_app) — it's
    # a plain local file write (os.urandom(32).hex(), mode 0600), not
    # anything that needs the gateway itself, so we can do it here too.
    # Without it the app's backend has no KIROCREW_PROXY_SECRET to
    # authenticate with, and the gateway proxy 502s every request to it —
    # including the notification API calls app/backend/notifications.py
    # needs for its whole reason for existing.
    log_info "Writing app secret..."
    local secret_file="$APP_DEST_DIR/.app_secret"
    if [ ! -f "$secret_file" ]; then
        (umask 077 && python3 -c 'import os; print(os.urandom(32).hex())' > "$secret_file")
        log_success "App secret generated"
    else
        log_info "App secret already present — left untouched"
    fi

    log_info "Setting up backend virtualenv..."
    local venv_ok=1
    provision_venv "$APP_DEST_DIR" || venv_ok=0

    echo
    log_success "KiroCrew Sync app staged at $APP_DEST_DIR"
    echo
    echo "This offline install wrote correct app files, installed.json, and"
    echo ".app_secret — matching what the gateway's own installer produces —"
    echo "but it could NOT register agents/skills/crons or start the backend;"
    echo "only a running gateway can do that."
    echo
    if [ "$venv_ok" -eq 0 ]; then
        log_warn "Backend venv setup failed (see above) — the backend will NOT start until"
        log_warn "this is fixed, even after you Enable the app from the dashboard."
    fi
    print_trust_and_enable_steps
}

# ---------------------------------------------------------------------------
# Install / uninstall entry points
# ---------------------------------------------------------------------------

do_install() {
    log_info "Installing KiroCrew Sync app..."
    echo

    check_prerequisites

    if ! command -v curl >/dev/null 2>&1; then
        log_warn "curl not found — cannot use the gateway API."
        echo
        do_offline_install
        return
    fi

    if gateway_reachable; then
        log_info "KiroCrew gateway detected at $GATEWAY_BASE"
        if install_via_gateway; then
            echo
            # The gateway API call above did the file copy into $APP_DEST_DIR;
            # the venv is machine-specific and the gateway does not create it,
            # so it must be provisioned here, against the destination it just
            # wrote to, before the app is ever enabled and its backend spawned.
            log_info "Setting up backend virtualenv..."
            local venv_ok=1
            provision_venv "$APP_DEST_DIR" || venv_ok=0
            echo
            log_success "KiroCrew Sync app installed."
            echo
            if [ "$venv_ok" -eq 0 ]; then
                log_warn "Backend venv setup failed (see above) — the backend will NOT start"
                log_warn "until this is fixed, even after you Enable the app from the dashboard."
                echo
            fi
            print_trust_and_enable_steps
            return
        fi
        echo
        log_warn "Automatic install via the gateway did not complete."
        print_manual_install_instructions
        exit 1
    fi

    log_warn "KiroCrew gateway not reachable at $GATEWAY_BASE — falling back to an offline install."
    log_warn "An offline install stages files and metadata correctly but cannot register"
    log_warn "resources or start the backend; that only happens once you Enable the app"
    log_warn "from a running gateway's dashboard."
    echo
    do_offline_install
}

do_uninstall() {
    if [ ! -d "$APP_DEST_DIR" ]; then
        log_warn "Nothing to do: $APP_DEST_DIR does not exist"
        exit 0
    fi

    if gateway_reachable; then
        log_info "KiroCrew gateway detected — uninstalling via POST /api/apps/$APP_NAME/uninstall"
        local token resp http_status http_body
        if token="$(mint_gateway_token)" && [ -n "$token" ]; then
            resp="$(curl -sS --max-time 60 -w '\n%{http_code}' -X POST \
                -H 'Content-Type: application/json' \
                -d '{"purge_data": true}' \
                "$GATEWAY_BASE/api/apps/$APP_NAME/uninstall?token=$token")"
            http_status="${resp##*$'\n'}"
            http_body="${resp%$'\n'*}"
            if [ "$http_status" = "200" ]; then
                log_success "Uninstalled via the gateway (resources deregistered, backend stopped)"
                echo
                log_info "This did not touch $SYNC_DIR."
                return 0
            fi
            log_warn "Gateway uninstall failed (HTTP $http_status): $(printf '%s' "$http_body" | head -c 300)"
            log_warn "Falling back to removing the app directory directly."
        else
            log_warn "Could not authenticate to the gateway automatically."
            log_warn "If the app is currently enabled, its registered agents/skills/crons and"
            log_warn "backend will be left stale until you disable it from the dashboard — removing"
            log_warn "files directly does not deregister anything."
        fi
    fi

    log_info "Removing KiroCrew Sync app from $APP_DEST_DIR..."
    rm -rf "$APP_DEST_DIR"
    log_success "App directory removed"
    echo
    log_info "This only removed the installed app files (including its history"
    log_info "database). It did not touch $SYNC_DIR."
    log_info "Remember to also disable/remove it in Settings → Apps, then"
    log_info "restart the KiroCrew gateway."
}

MODE="${1:-install}"
case "$MODE" in
    install)
        do_install
        ;;
    --uninstall)
        do_uninstall
        ;;
    -h|--help)
        usage
        ;;
    *)
        log_error "Unknown option: $MODE"
        usage
        exit 1
        ;;
esac
