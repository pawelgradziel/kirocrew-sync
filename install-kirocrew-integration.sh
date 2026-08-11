#!/usr/bin/env bash
#
# Install kirocrew-sync integration into KiroCrew
#
# Usage:
#   ./install-kirocrew-integration.sh [--skill-only|--cron|--daemon]
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${BLUE}ℹ${NC} $*"; }
log_success() { echo -e "${GREEN}✓${NC} $*"; }
log_warn() { echo -e "${YELLOW}⚠${NC} $*"; }

MODE="${1:-all}"

install_skill() {
    log_info "Installing KiroCrew skill..."
    
    local skill_dir="$HOME/.kiro/crew/skills/kirocrew-sync"
    mkdir -p "$skill_dir"
    
    cp "$PROJECT_ROOT/contrib/kirocrew-skill/SKILL.md" "$skill_dir/SKILL.md"
    log_success "Skill installed to $skill_dir"
    log_info "You can now say: 'sync my data' or 'check sync status' in chat"
}

install_cron() {
    log_info "Installing KiroCrew cron integration..."
    
    local cron_dir="$HOME/.kiro/crew/crons"
    mkdir -p "$cron_dir"
    
    cp "$PROJECT_ROOT/contrib/kirocrew-cron/sync_daemon.py" "$cron_dir/sync_daemon.py"
    log_success "Cron script installed to $cron_dir"
    
    echo
    log_info "To register the cron job:"
    echo "  kirocrew cron add 'kirocrew-sync' '{\"team\": false}' \\"
    echo "    --every 300 \\"
    echo "    --script '~/.kiro/crew/crons/sync_daemon.py:sync' \\"
    echo "    --approval-mode auto"
    echo
    log_info "For team scope, change {\"team\": false} to {\"team\": true}"
}

install_daemon() {
    log_info "Installing system daemon..."
    
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        local plist="$HOME/Library/LaunchAgents/com.kirocrew.sync.plist"
        cp "$PROJECT_ROOT/contrib/launchd/com.kirocrew.sync.plist" "$plist"
        log_success "LaunchAgent installed to $plist"
        
        echo
        log_info "To start the daemon:"
        echo "  launchctl load ~/Library/LaunchAgents/com.kirocrew.sync.plist"
        echo
        log_info "Check status:"
        echo "  launchctl list | grep kirocrew"
        echo "  tail -f /tmp/kirocrew-sync.log"
        
    else
        # Linux (systemd)
        local service_dir="$HOME/.config/systemd/user"
        mkdir -p "$service_dir"
        cp "$PROJECT_ROOT/contrib/systemd/kirocrew-sync.service" "$service_dir/"
        log_success "Systemd service installed to $service_dir"
        
        echo
        log_info "To start the daemon:"
        echo "  systemctl --user enable kirocrew-sync.service"
        echo "  systemctl --user start kirocrew-sync.service"
        echo
        log_info "Check status:"
        echo "  systemctl --user status kirocrew-sync.service"
        echo "  journalctl --user -u kirocrew-sync.service -f"
    fi
}

case "$MODE" in
    --skill-only)
        install_skill
        ;;
    --cron)
        install_skill
        install_cron
        ;;
    --daemon)
        install_skill
        install_daemon
        ;;
    all|--all)
        install_skill
        echo
        log_info "Choose ONE background option:"
        echo
        echo "  Option 1: KiroCrew cron (cross-platform, dashboard-managed)"
        echo "    ./install-kirocrew-integration.sh --cron"
        echo
        echo "  Option 2: System daemon (adaptive intervals, runs always)"
        echo "    ./install-kirocrew-integration.sh --daemon"
        ;;
    *)
        echo "Usage: $0 [--skill-only|--cron|--daemon|--all]"
        exit 1
        ;;
esac

echo
log_success "Installation complete!"
log_info "See docs/kirocrew-integration.md for full details"
