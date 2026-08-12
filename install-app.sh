#!/usr/bin/env bash
#
# Install (or uninstall) the KiroCrew Sync app into KiroCrew.
#
# Usage:
#   ./install-app.sh              Install/update the app
#   ./install-app.sh --uninstall  Remove the installed app
#   ./install-app.sh --help       Show this help
#
# Safe to re-run: each install pass copies fresh files over the existing
# install directory, re-initializes the (non-destructive) database schema,
# and refreshes installation metadata without touching sync history.

set -euo pipefail

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${BLUE}ℹ${NC} $*"; }
log_success() { echo -e "${GREEN}✓${NC} $*"; }
log_warn() { echo -e "${YELLOW}⚠${NC} $*"; }
log_error() { echo -e "${RED}✗${NC} $*" >&2; }

# Paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SRC_DIR="$SCRIPT_DIR/app"
APP_DEST_DIR="$HOME/.kiro/crew/apps/kirocrew-sync"
SYNC_DIR="$HOME/.kiro/crew/workspace/kirocrew-sync"

usage() {
    cat <<EOF
Usage: $(basename "${BASH_SOURCE[0]}") [--uninstall|--help]

  (no args)     Install or update the app at $APP_DEST_DIR
  --uninstall   Remove the app directory (does not touch $SYNC_DIR)
  --help        Show this help
EOF
}

# Verify everything the install needs is present *before* copying anything,
# so a missing prerequisite fails loudly instead of leaving a half-installed
# app directory behind.
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
}

do_install() {
    log_info "Installing KiroCrew Sync app..."
    echo

    check_prerequisites

    # Create app directory structure. cp -r below will create any deeper
    # subdirectories (ui/components, ui/assets, ...) as needed.
    log_info "Creating app directory..."
    mkdir -p "$APP_DEST_DIR"/{backend,data,ui}

    # Copy files. Whole directories are copied (not individual filenames) so
    # new backend modules or UI assets are never silently left behind.
    log_info "Copying app files..."
    cp "$APP_SRC_DIR/app.json" "$APP_DEST_DIR/"
    cp -r "$APP_SRC_DIR/backend"/. "$APP_DEST_DIR/backend/"
    cp -r "$APP_SRC_DIR/ui"/. "$APP_DEST_DIR/ui/"

    if [ -f "$APP_SRC_DIR/requirements.txt" ]; then
        cp "$APP_SRC_DIR/requirements.txt" "$APP_DEST_DIR/"
    fi

    # Drop any Python bytecode cache that tagged along from the source
    # checkout; it is regenerated automatically and should not ship.
    find "$APP_DEST_DIR/backend" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

    log_success "App files copied"

    # Initialize database. database.py uses CREATE TABLE IF NOT EXISTS /
    # INSERT OR IGNORE, so re-running this on an existing install is safe
    # and never wipes sync history.
    log_info "Initializing database..."
    python3 "$APP_DEST_DIR/backend/database.py"

    # Create/refresh installation metadata. Preserve the original
    # installedAt across re-runs and record the latest update separately.
    log_info "Recording installation metadata..."
    local now installed_at existing
    now="$(date -Iseconds)"
    installed_at="$now"
    existing=""
    if [ -f "$APP_DEST_DIR/installed.json" ]; then
        existing="$(python3 -c "
import json
try:
    with open('$APP_DEST_DIR/installed.json') as f:
        print(json.load(f).get('installedAt', ''))
except Exception:
    print('')
" 2>/dev/null || true)"
        if [ -n "$existing" ]; then
            installed_at="$existing"
        fi
    fi

    cat > "$APP_DEST_DIR/installed.json" << EOF
{
  "installedAt": "$installed_at",
  "updatedAt": "$now",
  "version": "1.0.0",
  "source": "local"
}
EOF

    log_success "Installation metadata recorded"

    echo
    log_success "KiroCrew Sync app installed at $APP_DEST_DIR"
    echo
    echo "Next steps:"
    echo "  1. Restart KiroCrew gateway (if running)"
    echo "  2. Go to Settings → Apps → kirocrew-sync"
    echo "  3. Enable the app"
    echo "  4. Navigate to /apps/kirocrew-sync in dashboard"
    echo
    log_info "After enabling, the sync daemon will start automatically"
}

do_uninstall() {
    if [ ! -d "$APP_DEST_DIR" ]; then
        log_warn "Nothing to do: $APP_DEST_DIR does not exist"
        exit 0
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
