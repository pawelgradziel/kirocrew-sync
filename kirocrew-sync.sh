#!/usr/bin/env bash
#
# KiroCrew Sync - three-way sync with modular storage backends
# Supports: Linux, macOS
# Storage backends: Google Drive, S3, rsync, local directory
#
# Sync works on an unpacked, canonical form of KiroCrew's state rather than on
# the raw files. Databases become one JSONL file per table; git tracks that
# form, finds the merge base, and drives row-level merge drivers. See
# docs/adr/0002-three-way-sync-via-unpacked-git-repo.md
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${KIROCREW_SYNC_CONFIG:-$SCRIPT_DIR/config.sh}"

# Captured before config.sh is sourced so an explicit environment variable
# still overrides the config file rather than the other way around.
ENV_SYNC_BACKEND="${SYNC_BACKEND:-}"
ENV_KIROCREW_DIR="${KIROCREW_DIR:-}"
ENV_SYNC_PORTABLE_PATHS="${SYNC_PORTABLE_PATHS:-}"
ENV_KIROCREW_PATH_MAP="${KIROCREW_PATH_MAP:-}"
ENV_SYNC_SCOPE="${SYNC_SCOPE:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}ℹ${NC} $*"; }
log_success() { echo -e "${GREEN}✓${NC} $*"; }
log_warn()    { echo -e "${YELLOW}⚠${NC} $*"; }
log_error()   { echo -e "${RED}✗${NC} $*"; }

if [ -f "$CONFIG_FILE" ]; then
    # shellcheck source=/dev/null
    source "$CONFIG_FILE"
else
    log_warn "Config file not found at $CONFIG_FILE"
    log_info "Run: ./kirocrew-sync.sh init"
    exit 1
fi

BACKEND="${ENV_SYNC_BACKEND:-${SYNC_BACKEND:-gdrive}}"
KIROCREW_DIR="${ENV_KIROCREW_DIR:-${KIROCREW_DIR:-$HOME/.kiro/crew}}"

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
# verbatim instead. See docs/adr/0001-knowledge-path-portability.md
SYNC_PORTABLE_PATHS="${ENV_SYNC_PORTABLE_PATHS:-${SYNC_PORTABLE_PATHS:-1}}"
KIROCREW_PATH_MAP="${ENV_KIROCREW_PATH_MAP:-${KIROCREW_PATH_MAP:-$KIROCREW_DIR/path_map.conf}}"
export KIROCREW_PATH_MAP SYNC_PORTABLE_PATHS

# Sync scope. "personal" is your own machines and syncs everything syncable;
# "team" shares a library with colleagues and publishes only what is marked
# shared -- no transcripts, no per-person config. Off by default; set here or
# with --team, and the environment still wins over config.sh.
SYNC_SCOPE="${ENV_SYNC_SCOPE:-${SYNC_SCOPE:-personal}}"

SYNC_ROOT="$KIROCREW_DIR/.sync"
SYNC_BRANCH="main"

# Each scope gets its own repo. They hold different subsets of the same data,
# so sharing one would make switching scope look like a mass deletion, and
# that deletion would propagate to every other machine on the next sync.
scope_paths() {
    if [ "$SYNC_SCOPE" = "team" ]; then
        SYNC_REPO="$SYNC_ROOT/repo-team"
        CONFLICT_LOG="$SYNC_ROOT/conflicts-team.jsonl"
        QUARANTINE_LOG="$SYNC_ROOT/quarantine-team.txt"
    else
        SYNC_REPO="$SYNC_ROOT/repo"
        CONFLICT_LOG="$SYNC_ROOT/conflicts.jsonl"
        QUARANTINE_LOG="$SYNC_ROOT/quarantine.txt"
    fi
}
scope_paths

STRATEGY="auto"
DRY_RUN=false
FORCE=false

PYTHON_BIN="${KIROCREW_SYNC_PYTHON:-python3}"

# Scratch space for the transport payload. Script-level, not local to a
# command: the EXIT trap fires after the command function has returned, so a
# local would be out of scope by then -- and under `set -u` that is a fatal
# error that overwrites the real exit status.
TEMP_DIR=""
cleanup_temp() {
    [ -n "${TEMP_DIR:-}" ] && rm -rf "$TEMP_DIR"
    return 0
}
trap cleanup_temp EXIT

make_temp_dir() {
    TEMP_DIR="$(mktemp -d)"
}

kcsync() {
    PYTHONPATH="$SCRIPT_DIR/lib" "$PYTHON_BIN" -m kcsync "$@"
}

# Same, plus the active scope. Every subcommand that reads or writes KiroCrew
# data takes --scope; `conflicts` and `merge-driver` do not, and call kcsync
# directly.
kcsync_scoped() {
    kcsync "$@" --scope "$SYNC_SCOPE"
}

require_python() {
    if ! command -v "$PYTHON_BIN" &> /dev/null; then
        log_error "$PYTHON_BIN is not installed"
        log_info "The sync engine needs Python 3.8+ with the stdlib sqlite3 module."
        exit 1
    fi
    if ! "$PYTHON_BIN" -c 'import sqlite3, sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
        log_error "$PYTHON_BIN lacks sqlite3 or is older than 3.8"
        exit 1
    fi
}

require_git() {
    if ! command -v git &> /dev/null; then
        log_error "git is not installed"
        log_info "git provides the merge base and conflict handling for sync."
        exit 1
    fi
}

# Refuse to write merged data while KiroCrew is running. Reading is safe, so
# only the pack step is gated. The pattern matches this script too -- its own
# name contains "kirocrew" -- so self-matches are filtered out before deciding.
check_kirocrew_running() {
    if [ "${KIROCREW_SYNC_SKIP_RUNNING_CHECK:-0}" = "1" ]; then
        return 0
    fi

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
        log_error "KiroCrew is running. Stop it before writing merged data back."
        log_info "Running as: $args"
        log_info "Reading is safe; writing is not. Quit KiroCrew and re-run."
        return 1
    done < <(pgrep -f "kirocrew" 2>/dev/null || true)

    return 0
}

get_machine_id() {
    local id
    if [ -f "$KIROCREW_DIR/.machine_id" ]; then
        id="$(cat "$KIROCREW_DIR/.machine_id")"
    else
        mkdir -p "$KIROCREW_DIR"
        id="$(hostname)-$(date +%s)"
        echo "$id" > "$KIROCREW_DIR/.machine_id"
    fi
    # Used as a filename and a git ref component.
    echo "$id" | tr -c 'A-Za-z0-9._-' '-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//'
}

git_repo() {
    git -C "$SYNC_REPO" "$@"
}

ensure_repo() {
    require_git
    mkdir -p "$SYNC_ROOT"

    if [ ! -d "$SYNC_REPO/.git" ]; then
        mkdir -p "$SYNC_REPO"
        git init -q -b "$SYNC_BRANCH" "$SYNC_REPO"
        log_success "Created sync repo at $SYNC_REPO"
    fi

    local machine_id
    machine_id="$(get_machine_id)"
    git_repo config user.name  "kirocrew-sync"
    git_repo config user.email "kirocrew-sync@${machine_id}"
    # Sync commits are machine state, not authored work; never sign or hook them.
    git_repo config commit.gpgsign false
    git_repo config core.hooksPath /dev/null

    local driver="PYTHONPATH=$SCRIPT_DIR/lib $PYTHON_BIN -m kcsync merge-driver"
    git_repo config merge.kcsync-rows.name "KiroCrew row-level three-way merge"
    git_repo config merge.kcsync-rows.driver "$driver rows %O %A %B %P"
    git_repo config merge.kcsync-json.name "KiroCrew structural JSON merge"
    git_repo config merge.kcsync-json.driver "$driver json %O %A %B %P"
    # Built-in "keep ours" for regenerated files.
    git_repo config merge.ours.name "keep ours"
    git_repo config merge.ours.driver "true"

    cat > "$SYNC_REPO/.gitattributes" << 'EOF'
# Row-level three-way merge, keyed on each table's primary key.
db/**/*.jsonl           merge=kcsync-rows
# A schema difference means the machines are on different KiroCrew versions.
# Treating it as binary forces a hard conflict instead of a silent text merge.
db/*/_schema.sql        merge=binary
# Regenerated from the schema on every unpack.
db/*/_policy.json       merge=ours
# Structural merge, key by key.
files/**/*.json         merge=kcsync-json
# Session transcripts are append-only.
files/sessions/*.jsonl  merge=union
# Content-addressed: identical path implies identical bytes.
blob/**                 binary
* text=auto eol=lf
EOF
}

repo_has_commit() {
    git_repo rev-parse --verify -q "$SYNC_BRANCH" > /dev/null 2>&1
}

commit_local_state() {
    local message="$1"
    git_repo add -A
    if git_repo diff --cached --quiet 2>/dev/null && repo_has_commit; then
        return 1   # nothing changed
    fi
    git_repo commit -q -m "$message"
    return 0
}

bundle_name() {
    echo "$(get_machine_id).bundle"
}

# Fetch every machine's bundle into refs/remotes/<machine>/<branch>.
fetch_bundles() {
    local dir="$1"
    local own
    own="$(bundle_name)"
    local found=0

    shopt -s nullglob
    for bundle in "$dir"/bundles/*.bundle; do
        local base
        base="$(basename "$bundle" .bundle)"
        [ "$(basename "$bundle")" = "$own" ] && continue
        # This function's stdout is captured, so diagnostics go to stderr.
        if ! git_repo bundle verify "$bundle" > /dev/null 2>&1; then
            log_warn "Skipping unreadable bundle: $(basename "$bundle")" >&2
            continue
        fi
        if git_repo fetch -q "$bundle" \
            "refs/heads/$SYNC_BRANCH:refs/remotes/$base/$SYNC_BRANCH" 2>/dev/null; then
            found=$((found + 1))
        else
            log_warn "Could not fetch from $(basename "$bundle")" >&2
        fi
    done
    shopt -u nullglob
    echo "$found"
}

# Schema version and embedding space must be compared before merging: the
# merge collapses each row to one winner, after which the difference is gone.
check_remote_compat() {
    local ref="$1" machine="$2"
    local tmp
    tmp="$(mktemp -d)"
    if ! git_repo archive "$ref" db 2>/dev/null | tar -x -C "$tmp" 2>/dev/null; then
        rm -rf "$tmp"
        return 0
    fi
    # Legacy bundles predate the scope marker; absent means personal.
    local remote_scope
    remote_scope="$(git_repo show "$ref:.kcsync-scope" 2>/dev/null | tr -d '[:space:]')"
    [ -n "$remote_scope" ] || remote_scope="personal"

    local rc=0
    kcsync_scoped compat --kirocrew-dir "$KIROCREW_DIR" --remote "$tmp" \
        --remote-scope "$remote_scope" --label "$machine" || rc=$?
    rm -rf "$tmp"
    return $rc
}

merge_remote_refs() {
    local conflicted=0
    local merged=0
    local incompatible=0

    local refs
    refs="$(git_repo for-each-ref --format='%(refname)' "refs/remotes" 2>/dev/null || true)"
    [ -z "$refs" ] && { echo "0 0 0"; return 0; }

    while IFS= read -r ref; do
        [ -z "$ref" ] && continue
        local machine
        machine="$(echo "$ref" | cut -d/ -f3)"

        if git_repo merge-base --is-ancestor "$ref" "$SYNC_BRANCH" 2>/dev/null; then
            continue   # already have everything from this machine
        fi

        # Quarantine rather than abort: this machine's rows are simply not
        # merged, which leaves local data exactly as it was. The next sync
        # re-checks it, so it rejoins on its own once the versions line up.
        if ! $FORCE && ! check_remote_compat "$ref" "$machine"; then
            incompatible=$((incompatible + 1))
            echo "$machine" >> "$QUARANTINE_LOG"
            continue
        fi

        local extra=()
        if ! git_repo merge-base "$SYNC_BRANCH" "$ref" > /dev/null 2>&1; then
            # First sync between these machines: no shared history, so the
            # merge base is empty and every row is an add on both sides.
            extra+=(--allow-unrelated-histories)
        fi

        # This function's stdout is captured, so diagnostics go to stderr.
        log_info "Merging changes from $machine..." >&2
        # ${extra[@]+...} rather than "${extra[@]}": bash 3.2, still the system
        # bash on macOS, treats an empty array as unset under `set -u`.
        if KCSYNC_STRATEGY="$STRATEGY" \
           KCSYNC_REPO="$SYNC_REPO" \
           KCSYNC_CONFLICT_LOG="$CONFLICT_LOG" \
           git_repo merge -q --no-edit ${extra[@]+"${extra[@]}"} \
                -m "sync: merge $machine" "$ref" 2>/dev/null; then
            merged=$((merged + 1))
        else
            if [ -n "$(git_repo ls-files -u)" ]; then
                conflicted=$((conflicted + 1))
                break
            fi
            log_warn "Merge from $machine did not complete" >&2
        fi
    done <<< "$refs"

    echo "$merged $conflicted $incompatible"
}

report_conflicts() {
    [ -s "$CONFLICT_LOG" ] || return 0
    local total
    total="$(wc -l < "$CONFLICT_LOG" | tr -d ' ')"
    log_warn "$total row conflict(s) resolved automatically:"
    kcsync conflicts "$CONFLICT_LOG" || true
    log_info "Full log: $CONFLICT_LOG"
}

show_unresolved() {
    log_error "Unresolved conflicts. Sync stopped before touching your data."
    git_repo diff --name-only --diff-filter=U | sed 's/^/    /'
    log_info ""
    log_info "Resolve inside $SYNC_REPO, then:"
    log_info "  git -C '$SYNC_REPO' add -A && git -C '$SYNC_REPO' commit"
    log_info "  $0 resume"
    log_info ""
    log_info "Or re-run with an automatic strategy:"
    log_info "  git -C '$SYNC_REPO' merge --abort"
    log_info "  $0 sync --strategy local-wins    # or remote-wins"
}

apply_to_kirocrew() {
    local pack_args=(--kirocrew-dir "$KIROCREW_DIR" --repo "$SYNC_REPO")
    $DRY_RUN && pack_args+=(--dry-run)
    $FORCE   && pack_args+=(--force)

    if ! $DRY_RUN; then
        check_kirocrew_running || exit 1
    fi

    log_info "Applying merged state to KiroCrew..."
    if ! kcsync_scoped pack "${pack_args[@]}"; then
        log_error "Pack failed; KiroCrew data was left unchanged (or restored)."
        exit 1
    fi
}

publish_bundle() {
    local temp_dir="$1"
    mkdir -p "$temp_dir/bundles"
    git_repo bundle create "$temp_dir/bundles/$(bundle_name)" "$SYNC_BRANCH" \
        > /dev/null 2>&1
    log_info "Publishing to $BACKEND..."
    backend_push "$temp_dir"
}

# --------------------------------------------------------------------------

cmd_sync() {
    require_python
    ensure_repo
    : > "$CONFLICT_LOG"
    : > "$QUARANTINE_LOG"
    local quarantined=0

    if [ -n "$(git_repo ls-files -u 2>/dev/null)" ]; then
        log_error "A previous sync left unresolved conflicts."
        log_info "Resolve them and run '$0 resume', or 'git -C \"$SYNC_REPO\" merge --abort'."
        exit 1
    fi

    log_info "Unpacking local state..."
    kcsync_scoped unpack --kirocrew-dir "$KIROCREW_DIR" --repo "$SYNC_REPO"
    if commit_local_state "sync: local state from $(get_machine_id)"; then
        log_success "Recorded local changes"
    else
        log_info "No local changes since last sync"
    fi

    make_temp_dir

    log_info "Fetching from $BACKEND..."
    if ! backend_pull "$TEMP_DIR" 2>/dev/null; then
        log_warn "Nothing to pull (first sync, or remote not reachable)"
    fi

    local fetched
    fetched="$(fetch_bundles "$TEMP_DIR")"
    if [ "$fetched" -gt 0 ]; then
        log_success "Fetched $fetched remote machine(s)"
        local result merged conflicted incompatible
        result="$(merge_remote_refs)"
        read -r merged conflicted incompatible <<< "$result"

        if [ "$conflicted" -gt 0 ]; then
            report_conflicts
            show_unresolved
            exit 1
        fi
        [ "$merged" -gt 0 ] && log_success "Merged $merged remote machine(s)"

        # An incompatible machine is quarantined, not fatal. Its rows were
        # never merged, so nothing local is at risk -- and refusing to
        # continue would punish the machines that did merge cleanly. It is
        # re-checked every sync and rejoins by itself once both sides run the
        # same KiroCrew version and embedding model.
        if [ "${incompatible:-0}" -gt 0 ]; then
            quarantined=$incompatible
            log_warn "$incompatible machine(s) quarantined; their changes were not merged:"
            sed 's/^/    /' "$QUARANTINE_LOG"
            log_info "Nothing to do here: the next sync re-checks them and"
            log_info "merges automatically once the versions match. To merge"
            log_info "one now anyway, re-run with --force."
        fi
    else
        log_info "No remote machines found"
    fi

    report_conflicts
    apply_to_kirocrew

    if $DRY_RUN; then
        log_success "Dry run complete. Nothing was written or published."
        [ "$quarantined" -gt 0 ] && return 3
        return 0
    fi

    publish_bundle "$TEMP_DIR"
    if [ "$quarantined" -gt 0 ]; then
        log_warn "Sync complete, with $quarantined machine(s) still quarantined"
        return 3
    fi
    log_success "Sync complete"
}

cmd_resume() {
    require_python
    ensure_repo

    if [ -n "$(git_repo ls-files -u 2>/dev/null)" ]; then
        log_error "There are still unmerged paths in $SYNC_REPO"
        git_repo diff --name-only --diff-filter=U | sed 's/^/    /'
        exit 1
    fi
    if [ -f "$SYNC_REPO/.git/MERGE_HEAD" ]; then
        git_repo commit -q --no-edit
        log_success "Completed the pending merge"
    fi

    report_conflicts
    apply_to_kirocrew
    $DRY_RUN && { log_success "Dry run complete."; return 0; }

    make_temp_dir
    backend_pull "$TEMP_DIR" 2>/dev/null || true
    publish_bundle "$TEMP_DIR"
    log_success "Sync complete"
}

cmd_push() {
    require_python
    ensure_repo
    log_info "Unpacking local state..."
    kcsync_scoped unpack --kirocrew-dir "$KIROCREW_DIR" --repo "$SYNC_REPO"
    commit_local_state "push: local state from $(get_machine_id)" || \
        log_info "No local changes since last sync"

    if $DRY_RUN; then
        log_success "Dry run complete. Nothing was published."
        return 0
    fi

    make_temp_dir
    # Backends mirror with delete, so other machines' bundles must be carried
    # forward rather than dropped.
    backend_pull "$TEMP_DIR" 2>/dev/null || true
    publish_bundle "$TEMP_DIR"
    log_success "Push completed"
    log_info "This published local state without merging. Use 'sync' for two-way."
}

cmd_pull() {
    STRATEGY="remote-wins"
    log_info "Pull applies remote changes, preferring remote on conflict."
    cmd_sync
}

cmd_status() {
    require_python
    log_info "Machine ID: $(get_machine_id)"
    log_info "Backend:    $BACKEND"
    log_info "Scope:      $SYNC_SCOPE"
    log_info "Sync repo:  $SYNC_REPO"
    echo

    if [ -d "$SYNC_REPO/.git" ]; then
        if repo_has_commit; then
            local last
            last="$(git_repo log -1 --format='%cr (%h) %s' "$SYNC_BRANCH")"
            log_info "Last sync commit: $last"
        fi
        if [ -f "$SYNC_REPO/.git/MERGE_HEAD" ]; then
            # Unpacking now would overwrite the half-merged tree.
            log_error "A merge is in progress; run '$0 resume' after resolving."
            git_repo diff --name-only --diff-filter=U | sed 's/^/    /'
        else
            kcsync_scoped unpack --kirocrew-dir "$KIROCREW_DIR" --repo "$SYNC_REPO" \
                > /dev/null 2>&1 || true
            local changed
            changed="$(git_repo status --porcelain | wc -l | tr -d ' ')"
            if [ "$changed" -gt 0 ]; then
                log_warn "$changed path(s) changed locally since last sync:"
                git_repo status --porcelain | head -20 | sed 's/^/    /'
            else
                log_success "No local changes since last sync"
            fi
        fi
        # A quarantine is easy to miss in a long sync log, and it persists
        # until the other machine catches up, so surface it here too.
        if [ -s "$QUARANTINE_LOG" ]; then
            log_warn "Quarantined at the last sync (different KiroCrew version"
            log_warn "or embedding model; their changes were not merged):"
            sed 's/^/    /' "$QUARANTINE_LOG"
        fi
    else
        log_warn "No sync repo yet. Run: $0 sync"
    fi

    echo
    kcsync_scoped doctor --kirocrew-dir "$KIROCREW_DIR" || true
    echo
    backend_status
}

cmd_paths() {
    log_info "Knowledge source paths on this machine"

    require_python
    if [ -f "$KIROCREW_PATH_MAP" ]; then
        log_info "Path map: $KIROCREW_PATH_MAP"
    else
        log_info "Path map: none ($KIROCREW_PATH_MAP)"
    fi
    if [ "$SYNC_PORTABLE_PATHS" != "1" ]; then
        log_warn "SYNC_PORTABLE_PATHS=0 - paths sync verbatim"
    fi
    echo

    # "Nothing to check" is not the same answer as "everything is fine", so
    # kcsync distinguishes them: 0 all resolve, 1 needs attention, 2 no data.
    local rc=0
    kcsync_scoped paths --kirocrew-dir "$KIROCREW_DIR" || rc=$?
    case "$rc" in
        0) log_success "All source paths resolve on this machine" ;;
        2) log_warn "Nothing to check yet (see above)" ;;
        *) log_warn "Some source paths need attention (see above)" ;;
    esac
}

cmd_doctor() {
    require_python
    require_git
    kcsync_scoped doctor --kirocrew-dir "$KIROCREW_DIR" || true
    if [ -d "$SYNC_REPO/.git" ]; then
        echo
        log_info "Preflight gates:"
        kcsync_scoped gates --kirocrew-dir "$KIROCREW_DIR" --repo "$SYNC_REPO" || true
    fi
}

cmd_init() {
    log_info "Initializing KiroCrew Sync..."

    if [ ! -f "$CONFIG_FILE" ]; then
        cat > "$CONFIG_FILE" << 'EOF'
#!/usr/bin/env bash
# KiroCrew Sync Configuration

# Storage backend: gdrive, s3, rsync, local, or custom
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

    require_python
    ensure_repo

    log_success "Initialization complete"
    log_info "Next steps:"
    log_info "  1. Edit $CONFIG_FILE to set your backend"
    log_info "  2. Configure backend credentials in backends/\${SYNC_BACKEND}.sh"
    log_info "  3. Run: ./kirocrew-sync.sh doctor"
    log_info "  4. Run: ./kirocrew-sync.sh sync"
}

show_help() {
    cat << EOF
KiroCrew Sync - three-way data synchronization

Usage: $0 <command> [options]

Commands:
  init        Initialize configuration and the local sync repo
  sync        Two-way sync: merge local and remote changes
  push        Publish local state without merging
  pull        Sync, preferring remote changes on conflict
  resume      Finish a sync that stopped on conflicts
  status      Show pending changes and backend state
  doctor      Inspect local data and run preflight checks
  paths       Check whether knowledge source paths survive a sync
  help        Show this message

Options:
  --strategy <s>   Conflict resolution: auto (default), local-wins,
                   remote-wins, manual
  --dry-run        Analyze and merge, but do not write or publish
  --team           Share a library with colleagues instead of syncing your
                   own machines: publishes the knowledge base, artifacts,
                   tags and learned lessons, and holds back chat transcripts,
                   episodic memory and per-person config. Off by default.
                   Equivalent to --scope team, or SYNC_SCOPE=team in config.
  --force          Merge quarantined machines and pack even if a preflight
                   gate fails. Applies to every machine at once, so it is
                   not the way to work around a single lagging machine --
                   sync already skips those and carries on.

Exit codes:
  0   Success
  1   Sync stopped; your data was not changed
  3   Sync completed, but one or more machines stayed quarantined

Environment variables:
  SYNC_BACKEND          Storage backend: gdrive, s3, rsync, local (default: gdrive)
  KIROCREW_DIR          KiroCrew data directory (default: ~/.kiro/crew)
  KIROCREW_PATH_MAP     Path mapping file (default: \$KIROCREW_DIR/path_map.conf)
  SYNC_PORTABLE_PATHS   Rewrite knowledge paths for portability (default: 1)
  SYNC_SCOPE            personal (default) or team

Examples:
  $0 sync
  $0 sync --dry-run
  $0 sync --strategy local-wins
  $0 sync --team
  SYNC_BACKEND=s3 $0 sync

Conflicts are resolved per row, not per file. "auto" keeps the most
recently updated version of each row and never drops an edit in favour
of a deletion. See docs/adr/0002-three-way-sync-via-unpacked-git-repo.md

A machine on a different KiroCrew version or embedding model is
quarantined: its changes are skipped, everyone else still syncs, and it
rejoins on its own once it catches up. "$0 status" lists any. Machines
syncing at a different scope are quarantined the same way, so a personal
and a team machine can never merge into each other by accident.

Each scope keeps its own sync repo and merge base, so the same machine can
sync personally with one config and with a team using another.
EOF
}

COMMAND="${1:-help}"
shift || true

while [ $# -gt 0 ]; do
    case "$1" in
        --strategy)
            STRATEGY="${2:-auto}"; shift 2 ;;
        --strategy=*)
            STRATEGY="${1#*=}"; shift ;;
        --dry-run)  DRY_RUN=true; shift ;;
        --force)    FORCE=true; shift ;;
        --team)     SYNC_SCOPE="team"; scope_paths; shift ;;
        --scope)    SYNC_SCOPE="${2:-personal}"; scope_paths; shift 2 ;;
        --scope=*)  SYNC_SCOPE="${1#*=}"; scope_paths; shift ;;
        *)
            log_error "Unknown option: $1"; show_help; exit 1 ;;
    esac
done

case "$STRATEGY" in
    auto|local-wins|remote-wins|manual) ;;
    *) log_error "Unknown strategy: $STRATEGY"
       log_info  "Valid: auto, local-wins, remote-wins, manual"
       exit 1 ;;
esac

case "$SYNC_SCOPE" in
    personal|team) ;;
    *) log_error "Unknown scope: $SYNC_SCOPE"
       log_info  "Valid: personal (default), team"
       exit 1 ;;
esac

case "$COMMAND" in
    init)    cmd_init ;;
    sync)    cmd_sync ;;
    push)    cmd_push ;;
    pull)    cmd_pull ;;
    resume)  cmd_resume ;;
    status)  cmd_status ;;
    doctor)  cmd_doctor ;;
    paths)   cmd_paths ;;
    help|--help|-h) show_help ;;
    *)
        log_error "Unknown command: $COMMAND"
        show_help
        exit 1 ;;
esac
