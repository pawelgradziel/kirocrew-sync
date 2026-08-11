#!/usr/bin/env bash
#
# Install KiroCrew Sync app into KiroCrew
#

set -euo pipefail

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${BLUE}ℹ${NC} $*"; }
log_success() { echo -e "${GREEN}✓${NC} $*"; }
log_warn() { echo -e "${YELLOW}⚠${NC} $*"; }
log_error() { echo -e "${RED}✗${NC} $*"; }

# Paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SRC_DIR="$SCRIPT_DIR/app"
APP_DEST_DIR="$HOME/.kiro/crew/apps/kirocrew-sync"
SYNC_DIR="$HOME/.kiro/crew/workspace/kirocrew-sync"

log_info "Installing KiroCrew Sync app..."
echo

# Check prerequisites
if [ ! -d "$SYNC_DIR" ]; then
    log_error "kirocrew-sync not found at $SYNC_DIR"
    echo
    echo "Please install kirocrew-sync first:"
    echo "  cd ~/.kiro/crew/workspace"
    echo "  git clone https://github.com/pawelgradziel/kirocrew-sync.git"
    echo "  cd kirocrew-sync"
    echo "  ./kirocrew-sync.sh init"
    exit 1
fi

# Create app directory
log_info "Creating app directory..."
mkdir -p "$APP_DEST_DIR"/{backend,data,ui/components}

# Copy files
log_info "Copying app files..."
cp "$APP_SRC_DIR/app.json" "$APP_DEST_DIR/"
cp -r "$APP_SRC_DIR/backend"/* "$APP_DEST_DIR/backend/"

log_success "App files copied"

# Initialize database
log_info "Initializing database..."
python3 "$APP_DEST_DIR/backend/database.py"

# Create installed.json
log_info "Creating installation metadata..."
cat > "$APP_DEST_DIR/installed.json" << EOF
{
  "installedAt": "$(date -Iseconds)",
  "version": "1.0.0",
  "source": "local"
}
EOF

log_success "Installation metadata created"

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
