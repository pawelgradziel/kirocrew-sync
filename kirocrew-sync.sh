#!/usr/bin/env bash
#
# KiroCrew Sync - Cross-platform sync utility with modular storage backends
# Supports: Linux, macOS
# Storage backends: Google Drive, S3, rclone, rsync
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIROCREW_DIR="${KIROCREW_DIR:-$HOME/.kiro/crew}"
CONFIG_FILE="$SCRIPT_DIR/config.sh"
BACKEND="${SYNC_BACKEND:-gdrive}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}ℹ${NC} $*"
}

log_success() {
    echo -e "${GREEN}✓${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}⚠${NC} $*"
}

log_error() {
    echo -e "${RED}✗${NC} $*"
}

# Load configuration
if [ -f "$CONFIG_FILE" ]; then
    # shellcheck source=/dev/null
    source "$CONFIG_FILE"
else
    log_warn "Config file not found at $CONFIG_FILE"
    log_info "Run: ./kirocrew-sync.sh init"
    exit 1
fi

# Load storage backend
BACKEND_FILE="$SCRIPT_DIR/backends/${BACKEND}.sh"
if [ ! -f "$BACKEND_FILE" ]; then
    log_error "Backend '$BACKEND' not found at $BACKEND_FILE"
    exit 1
fi
# shellcheck source=/dev/null
source "$BACKEND_FILE"

# Path portability: knowledge-base paths are rewritten to a machine-independent
# form on the way out and back to this machine's paths on the way in, so folder
# sources keep working after a sync. Set SYNC_PORTABLE_PATHS=0 to sync URIs
# verbatim instead.
PORTABLE_PATHS_TOOL="$SCRIPT_DIR/lib/portable_paths.py"
SYNC_PORTABLE_PATHS="${SYNC_PORTABLE_PATHS:-1}"
KIROCREW_PATH_MAP="${KIROCREW_PATH_MAP:-$KIROCREW_DIR/path_map.conf}"
export KIROCREW_PATH_MAP

# Data sources to sync
declare -A DATA_SOURCES=(
    ["sessions"]="sessions"
    ["memory.db"]="memory.db"
    ["memory.db-wal"]="memory.db-wal"
    ["memory.db-shm"]="memory.db-shm"
    ["memory_index.db"]="memory_index.db"
    ["session_map.json"]="session_map.json"
    ["artifacts"]="data/artifacts.db"
    ["knowledge"]="workspace/knowledge"
    ["workspace_memory"]="workspace/memory"
    ["config.json"]="config.json"
    ["tags.json"]="tags.json"
    ["lessons.db"]="data/lessons.db"
)

# Refuse to sync while KiroCrew is running, so a half-written database never
# travels. The pattern matches this script too -- its own name contains
# "kirocrew" -- so self-matches are filtered out before deciding.
check_kirocrew_running() {
    local self
    self="$(basename "${BASH_SOURCE[0]}")"
    local pid args

    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        [ "$pid" != "$$" ] || continue
        args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
        case "$args" in
            "" | *"$self"*) continue ;;
        esac
        log_error "KiroCrew is currently running. Please stop it before syncing."
        log_info "Running as: $args"
        log_info "Run: pkill -f kirocrew"
        return 1
    done < <(pgrep -f "kirocrew" 2>/dev/null || true)

    return 0
}

get_machine_id() {
    if [ -f "$KIROCREW_DIR/.machine_id" ]; then
        cat "$KIROCREW_DIR/.machine_id"
    else
        local machine_id
        machine_id="$(hostname)-$(date +%s)"
        echo "$machine_id" > "$KIROCREW_DIR/.machine_id"
        echo "$machine_id"
    fi
}

knowledge_db_path() {
    # knowledge.db under a bundle directory or under KIROCREW_DIR
    echo "$1/${DATA_SOURCES[knowledge]}/knowledge.db"
}

# Rewrite the knowledge paths in a BUNDLE copy of the database. The live
# database is never touched: KiroCrew resolves source URIs with a bare
# Path(uri) and does not expand "~", so it needs real absolute paths locally.
# Portable paths exist only while the data is in transit.
translate_bundle_paths() {
    local bundle_dir="$1"
    local direction="$2"
    local db
    db="$(knowledge_db_path "$bundle_dir")"

    [ "$SYNC_PORTABLE_PATHS" = "1" ] || return 0
    [ -f "$db" ] || return 0

    if ! command -v python3 &> /dev/null; then
        log_warn "python3 not found - knowledge paths sent as-is (folder sources may break on other machines)"
        return 0
    fi

    if [ "$direction" = "encode" ]; then
        log_info "Making knowledge paths portable..."
    else
        log_info "Resolving knowledge paths for this machine..."
    fi

    if python3 "$PORTABLE_PATHS_TOOL" "$direction" "$db"; then
        log_success "Knowledge paths translated"
    else
        log_warn "Path translation failed - continuing with paths as stored"
    fi
}

# A SQLite database and its write-ahead log are a matched pair. A bundle that
# carries a checkpointed .db with no -wal beside it must not inherit this
# machine's older -wal, which SQLite would replay onto the incoming file.
drop_stale_wal() {
    local bundle_path="$1"
    local dest_path="$2"

    if [ -d "$bundle_path" ]; then
        local db
        while IFS= read -r db; do
            drop_stale_wal "$db" "$dest_path/${db#"$bundle_path"/}"
        done < <(find "$bundle_path" -type f -name '*.db')
        return 0
    fi

    case "$bundle_path" in
        *.db) ;;
        *) return 0 ;;
    esac

    local suffix
    for suffix in -wal -shm; do
        if [ ! -e "${bundle_path}${suffix}" ] && [ -e "${dest_path}${suffix}" ]; then
            rm -f "${dest_path}${suffix}"
            log_info "Removed stale $(basename "${dest_path}${suffix}")"
        fi
    done
}

create_manifest() {
    local manifest_file="$1"
    local machine_id
    machine_id=$(get_machine_id)
    
    cat > "$manifest_file" << EOF
{
  "machine_id": "$machine_id",
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "platform": "$(uname -s)",
  "files": {
EOF

    local first=true
    for key in "${!DATA_SOURCES[@]}"; do
        local src="${DATA_SOURCES[$key]}"
        local full_path="$KIROCREW_DIR/$src"
        
        if [ -e "$full_path" ]; then
            [ "$first" = false ] && echo "," >> "$manifest_file"
            first=false
            
            local hash
            if [ -d "$full_path" ]; then
                hash=$(find "$full_path" -type f -exec md5sum {} \; 2>/dev/null | sort | md5sum | cut -d' ' -f1 || echo "dir")
            else
                hash=$(md5sum "$full_path" 2>/dev/null | cut -d' ' -f1 || echo "missing")
            fi
            
            printf '    "%s": {"path": "%s", "hash": "%s"}' "$key" "$src" "$hash" >> "$manifest_file"
        fi
    done
    
    cat >> "$manifest_file" << EOF

  }
}
EOF
}

prepare_sync_bundle() {
    local bundle_dir="$1"
    
    log_info "Preparing sync bundle..."
    
    mkdir -p "$bundle_dir"
    
    # Copy each data source
    for key in "${!DATA_SOURCES[@]}"; do
        local src="${DATA_SOURCES[$key]}"
        local full_path="$KIROCREW_DIR/$src"
        
        if [ -e "$full_path" ]; then
            local dest="$bundle_dir/$src"
            mkdir -p "$(dirname "$dest")"
            
            if [ -d "$full_path" ]; then
                rsync -a --exclude="*.lock" --exclude="*.tmp" "$full_path/" "$dest/"
            else
                cp "$full_path" "$dest"
            fi
            log_success "Packaged: $src"
        else
            log_warn "Skipped (not found): $src"
        fi
    done
    
    # Create manifest
    create_manifest "$bundle_dir/manifest.json"
    log_success "Created manifest"
}

apply_sync_bundle() {
    local bundle_dir="$1"
    
    if [ ! -f "$bundle_dir/manifest.json" ]; then
        log_error "Invalid sync bundle: manifest.json missing"
        return 1
    fi
    
    log_info "Applying sync bundle..."
    
    # Show remote machine info
    local remote_machine
    remote_machine=$(grep -o '"machine_id": "[^"]*"' "$bundle_dir/manifest.json" | cut -d'"' -f4)
    local remote_time
    remote_time=$(grep -o '"timestamp": "[^"]*"' "$bundle_dir/manifest.json" | cut -d'"' -f4)
    
    log_info "Remote: $remote_machine at $remote_time"
    
    # Apply each data source
    for key in "${!DATA_SOURCES[@]}"; do
        local src="${DATA_SOURCES[$key]}"
        local source_path="$bundle_dir/$src"
        local dest_path="$KIROCREW_DIR/$src"
        
        if [ -e "$source_path" ]; then
            mkdir -p "$(dirname "$dest_path")"
            drop_stale_wal "$source_path" "$dest_path"

            if [ -d "$source_path" ]; then
                rsync -a --exclude="*.lock" --exclude="*.tmp" "$source_path/" "$dest_path/"
            else
                cp "$source_path" "$dest_path"
            fi
            log_success "Applied: $src"
        fi
    done
    
    log_success "Sync bundle applied"
}

cmd_push() {
    log_info "Starting push to $BACKEND..."
    
    check_kirocrew_running || exit 1
    
    local temp_dir
    temp_dir=$(mktemp -d)
    trap 'rm -rf "$temp_dir"' EXIT
    
    prepare_sync_bundle "$temp_dir"
    translate_bundle_paths "$temp_dir" encode

    # Call backend-specific push
    backend_push "$temp_dir"
    
    log_success "Push completed"
}

cmd_pull() {
    log_info "Starting pull from $BACKEND..."
    
    check_kirocrew_running || exit 1
    
    local temp_dir
    temp_dir=$(mktemp -d)
    trap 'rm -rf "$temp_dir"' EXIT
    
    # Call backend-specific pull
    backend_pull "$temp_dir"
    
    if [ -f "$temp_dir/manifest.json" ]; then
        translate_bundle_paths "$temp_dir" decode
        apply_sync_bundle "$temp_dir"
        log_success "Pull completed"
    else
        log_error "Pull failed: no data received"
        exit 1
    fi
}

cmd_status() {
    log_info "Checking sync status..."
    
    local machine_id
    machine_id=$(get_machine_id)
    log_info "Machine ID: $machine_id"
    log_info "Backend: $BACKEND"
    
    # Call backend-specific status
    backend_status
    
    # Show local data status
    log_info "\nLocal data status:"
    for key in "${!DATA_SOURCES[@]}"; do
        local src="${DATA_SOURCES[$key]}"
        local full_path="$KIROCREW_DIR/$src"
        
        if [ -e "$full_path" ]; then
            if [ -d "$full_path" ]; then
                local count
                count=$(find "$full_path" -type f | wc -l)
                echo "  ✓ $key ($count files)"
            else
                local size
                size=$(du -h "$full_path" | cut -f1)
                echo "  ✓ $key ($size)"
            fi
        else
            echo "  ✗ $key (not found)"
        fi
    done
}

cmd_paths() {
    local db
    db="$(knowledge_db_path "$KIROCREW_DIR")"

    log_info "Knowledge source paths on this machine"

    if ! command -v python3 &> /dev/null; then
        log_error "python3 is required for path checks"
        exit 1
    fi
    if [ ! -f "$db" ]; then
        log_warn "No knowledge database at $db"
        return 0
    fi

    if [ -f "$KIROCREW_PATH_MAP" ]; then
        log_info "Path map: $KIROCREW_PATH_MAP"
    else
        log_info "Path map: none ($KIROCREW_PATH_MAP)"
    fi
    echo

    if python3 "$PORTABLE_PATHS_TOOL" report "$db"; then
        log_success "All source paths resolve on this machine"
    else
        log_warn "Some source paths need attention (see above)"
    fi
}

cmd_init() {
    log_info "Initializing KiroCrew Sync..."
    
    # Create config file
    if [ ! -f "$CONFIG_FILE" ]; then
        cat > "$CONFIG_FILE" << 'EOF'
#!/usr/bin/env bash
# KiroCrew Sync Configuration

# Storage backend: gdrive, s3, rclone, rsync
export SYNC_BACKEND="gdrive"

# KiroCrew data directory (override if needed)
export KIROCREW_DIR="$HOME/.kiro/crew"

# Rewrite knowledge folder paths so they survive a sync (0 to disable)
export SYNC_PORTABLE_PATHS=1

# This machine's own locations, for folders outside $HOME or laid out
# differently here (see path_map.conf.example)
export KIROCREW_PATH_MAP="$KIROCREW_DIR/path_map.conf"

# Backend-specific settings (edit backends/*.sh for credentials)
EOF
        log_success "Created config file: $CONFIG_FILE"
    fi
    
    # Create backends directory
    mkdir -p "$SCRIPT_DIR/backends"
    
    log_success "Initialization complete"
    log_info "Next steps:"
    log_info "  1. Edit $CONFIG_FILE to set your backend"
    log_info "  2. Configure backend credentials in backends/\${SYNC_BACKEND}.sh"
    log_info "  3. Run: ./kirocrew-sync.sh push"
}

show_help() {
    cat << EOF
KiroCrew Sync - Cross-platform data synchronization

Usage: $0 <command>

Commands:
  init        Initialize configuration
  push        Push local data to remote storage
  pull        Pull remote data to local machine
  status      Show sync status and local data info
  paths       Check whether knowledge source paths survive a sync
  help        Show this help message

Environment variables:
  SYNC_BACKEND          Storage backend to use (default: gdrive)
  KIROCREW_DIR          KiroCrew data directory (default: ~/.kiro/crew)
  KIROCREW_PATH_MAP     Path mapping file (default: \$KIROCREW_DIR/path_map.conf)
  SYNC_PORTABLE_PATHS   Rewrite knowledge paths for portability (default: 1)

Examples:
  $0 init
  $0 push
  SYNC_BACKEND=s3 $0 pull
  $0 status
  $0 paths

EOF
}

# Main command dispatcher
case "${1:-help}" in
    init)
        cmd_init
        ;;
    push)
        cmd_push
        ;;
    pull)
        cmd_pull
        ;;
    status)
        cmd_status
        ;;
    paths)
        cmd_paths
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        log_error "Unknown command: $1"
        show_help
        exit 1
        ;;
esac
