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
    # `init` is what CREATES config.sh, and `help` must work before anything is
    # set up at all -- so requiring the config here made both unreachable and
    # turned the message below into a catch-22: it told you to run `init`, and
    # this same guard then refused to run it. Every other command genuinely
    # needs the config, so they still stop here. The defaults below
    # (BACKEND=gdrive, KIROCREW_DIR=$HOME/.kiro/crew) are enough for init and
    # help to work unconfigured.
    case "${1:-}" in
        init|help|--help|-h) ;;
        *)
            log_warn "Config file not found at $CONFIG_FILE"
            log_info "Run: ./kirocrew-sync.sh init"
            exit 1
            ;;
    esac
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

# Touched after every successful pack, so check_kirocrew_running() can tell
# KiroCrew's database writes apart from the ones our own pack just made.
PACK_MARKER="$SYNC_ROOT/.last_pack"
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

# Source daemon implementation. Must come AFTER SYNC_ROOT is set: lib/daemon.sh
# derives DAEMON_STATE/DAEMON_LOCK from it at *source* time, so sourcing it any
# earlier aborted the whole script under `set -u` with
# "lib/daemon.sh: line 31: SYNC_ROOT: unbound variable" -- on every command,
# not just `daemon`. Nothing above this point calls into daemon.sh; its
# functions are only reached from the dispatch table at the end.
# shellcheck source=lib/daemon.sh
source "$SCRIPT_DIR/lib/daemon.sh"

STRATEGY="auto"
DRY_RUN=false
FORCE=false
EXPORT_OUTPUT=""
IMPORT_ARCHIVE=""
IMPORT_MODE=""

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

make_temp_dir() {
    TEMP_DIR="$(mktemp -d)"
}

# --------------------------------------------------------------------------
# Sync lock -- serializes cmd_sync/cmd_resume/cmd_push against each other.
#
# Without this, SIGKILLing lib/daemon.sh's daemon leaves any
# `kirocrew-sync.sh sync` child it had spawned running orphaned, and the
# KiroCrew app's POST /api/sync (SyncManager.is_running(), a `pgrep`) can
# start a second one on top of it. Both would share one git repo
# ($SYNC_REPO), one conflicts.jsonl and one quarantine.txt for the scope --
# and cmd_sync() truncates those two log files at the very start of every
# run, so an overlapping pair can destroy each other's conflict/quarantine
# evidence and interleave `git merge`/`git commit` on the same working tree.
#
# Scope: one lock PER SYNC SCOPE, not one global lock for the whole script.
# scope_paths() gives "personal" and "team" entirely disjoint $SYNC_REPO,
# $CONFLICT_LOG and $QUARANTINE_LOG (kirocrew-sync.sh:93-104) precisely so
# switching scope never looks like a mass deletion to the other scope. A
# personal sync and a team sync therefore touch no files in common, and a
# single script-wide lock would serialize two operations that can safely run
# at once -- e.g. the daemon polling personal while the app triggers a
# manual team sync. The lock file lives under $SYNC_ROOT (shared) but is
# named per scope so it inherits that same independence.
#
# Stale-lock handling (PID in the file, `kill -0` liveness, clean up if the
# holder is dead) mirrors lib/daemon.sh's acquire_daemon_lock() -- same
# convention, so anyone who already knows that pattern recognizes this one --
# but is NOT implemented by editing that file; lib/daemon.sh's lock protects
# a different resource (one daemon process per machine) and is owned
# elsewhere. The acquire step below is deliberately stronger than
# acquire_daemon_lock()'s plain check-then-write, which has a TOCTOU race
# between "lock file absent" and "write my PID": harmless for a daemon a
# human starts by hand, but this lock also guards POST /api/sync, where two
# requests can arrive close enough together to both pass a naive check
# before either writes. `set -C` (noclobber) makes the write an atomic
# O_EXCL create instead, so two racing acquires can never both succeed.
SYNC_LOCK_PID=""
SYNC_LOCK_PATH=""

sync_lock_path() {
    echo "$SYNC_ROOT/sync-${SYNC_SCOPE}.lock"
}

acquire_sync_lock() {
    mkdir -p "$SYNC_ROOT"
    local lock_file
    lock_file="$(sync_lock_path)"

    local attempt
    for attempt in 1 2; do
        if ( set -C; echo "$$" > "$lock_file" ) 2>/dev/null; then
            SYNC_LOCK_PID=$$
            SYNC_LOCK_PATH="$lock_file"
            return 0
        fi

        local lock_pid
        lock_pid="$(cat "$lock_file" 2>/dev/null || echo "")"
        if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
            log_error "Another sync (scope: $SYNC_SCOPE) is already running, PID $lock_pid"
            log_info "Wait for it to finish and try again."
            log_info "If you're certain it's dead: rm '$lock_file'"
            exit 1
        fi

        log_warn "Cleaning stale sync lock (PID ${lock_pid:-unknown} not running)"
        rm -f "$lock_file"
        # Loop back and retry the atomic create once. A second failure here
        # means another process won the race in the gap between our cleanup
        # and our retry -- rare, but still handled below rather than assumed
        # away.
    done

    log_error "Could not acquire sync lock at $lock_file"
    exit 1
}

release_sync_lock() {
    [ -n "$SYNC_LOCK_PATH" ] || return 0
    # Only remove it if it still names the PID we wrote -- guards against
    # deleting a fresh lock some other process legitimately acquired after a
    # stale-lock cleanup raced with us (see acquire_sync_lock()'s retry).
    local current
    current="$(cat "$SYNC_LOCK_PATH" 2>/dev/null || echo "")"
    if [ "$current" = "$SYNC_LOCK_PID" ]; then
        rm -f "$SYNC_LOCK_PATH"
    fi
    SYNC_LOCK_PATH=""
}

on_exit() {
    cleanup_temp
    release_sync_lock
}
trap on_exit EXIT

# A trap registered only for EXIT already fires on receipt of INT/TERM too,
# *as long as those signals have no trap of their own* -- bash still runs
# the EXIT trap and then dies from the signal. But this repo already has a
# real bug from assuming that generalizes: lib/daemon.sh used to register
# `trap release_daemon_lock EXIT INT TERM` (one handler, all three signals),
# and with an *explicit* trap on INT/TERM, bash runs the handler and then
# RESUMES the script instead of dying -- the daemon survived the signal
# having already released its lock, and a second daemon could then acquire
# it while the first kept running. The fix there (lib/daemon.sh:197-198,
# not touched by this file) was a *separate* INT/TERM trap whose handler
# ends with `exit`. Mirror that fix here too, rather than relying on the
# no-explicit-trap default above, so this stays correct even if some future
# change to this file ever adds an INT/TERM trap elsewhere.
trap 'on_exit; exit 130' INT
trap 'on_exit; exit 143' TERM

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
    local pid args running_as=""

    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        [ "$pid" != "$$" ] || continue
        args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
        case "$args" in
            "" | *"$self"*) continue ;;
        esac
        running_as="$args"
        break
    done < <(pgrep -f "kirocrew" 2>/dev/null || true)

    # Nothing else running: unambiguously safe.
    [ -n "$running_as" ] || return 0

    # KiroCrew is up. Refusing outright made this unusable from the KiroCrew
    # *app*, which by definition only ever runs with KiroCrew up -- so "Sync
    # Now" in the dashboard could never apply anything. It also contradicted
    # lib/daemon.sh, whose should_sync_now() is documented as "run only if
    # KiroCrew is closed, or if it's open but idle": the daemon would decide a
    # sync was safe and then be refused here anyway.
    #
    # Use that same idleness heuristic. An open-but-idle KiroCrew is not
    # mid-transaction, so packing is safe; one that wrote a database in the
    # last minute may well be, and is still refused.
    # Only writes NEWER than our own last pack count. A successful sync writes
    # KiroCrew's databases itself, so counting every recent write made a sync
    # poison the following minute: run N succeeds, and runs N+1/N+2 are refused
    # because they see run N's own writes and read them as "KiroCrew is busy".
    # Observed exactly that -- one sync merging 400 rows, then two refusals 30s
    # and 44s later. The marker is touched after each successful pack, so
    # anything not newer than it is our own work, not KiroCrew's.
    # Two whole subtrees under $KIROCREW_DIR are OURS, not KiroCrew's, and
    # counting them made this refuse forever:
    #   $SYNC_ROOT/**      the sync repo and the pre-pack backups this script
    #                      writes itself on every run
    #   $KIROCREW_DIR/apps/**  app-private databases -- including this app's own
    #                      data/history.db, which records a row for every sync,
    #                      so merely logging run N guaranteed run N+1 saw a
    #                      "recent write" and refused. Observed live: five
    #                      consecutive refusals with nothing but our own two
    #                      files being counted.
    # Measured with these excluded, KiroCrew's real databases do fall quiet
    # within ~20s of activity, which is what makes the heuristic workable.
    local find_args=(
        -path "$SYNC_ROOT/*" -prune -o
        -path "$KIROCREW_DIR/apps/*" -prune -o
        -name "*.db" -mmin -1
    )
    [ -f "$PACK_MARKER" ] && find_args+=(-newer "$PACK_MARKER")
    find_args+=(-print)

    local recent_writes
    recent_writes="$(find "$KIROCREW_DIR" "${find_args[@]}" 2>/dev/null | wc -l | tr -d ' ')"
    if [ "${recent_writes:-0}" -eq 0 ]; then
        log_warn "KiroCrew is running but idle; applying merged data anyway."
        log_info "Running as: $running_as"
        return 0
    fi

    log_error "KiroCrew is running and actively writing. Leaving your data alone."
    log_info "Running as: $running_as"
    log_info "$recent_writes database file(s) changed in the last minute."
    log_info "Retry shortly, or quit KiroCrew for a guaranteed-clean write."
    return 1
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
# `**/` so a member memory store (db/memory_stores/<name>/) is covered too.
db/**/_schema.sql       merge=binary
# Regenerated from the schema on every unpack.
db/**/_policy.json      merge=ours
# A memory store's full DDL, used only to create it on a machine that lacks
# it. Regenerated on every unpack; the drift gate compares _schema.sql.
db/**/_ddl.json         merge=ours
# Structural merge, key by key.
files/**/*.json         merge=kcsync-json
# Session transcripts are append-only. The archive segments a long
# conversation rolls its older turns into are too, and `*` does not cross '/',
# so they need their own line to get the same driver.
files/sessions/*.jsonl  merge=union
files/sessions/archive/*.jsonl  merge=union
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
        #
        # stdout must be discarded too, not just stderr: `git merge -q` still
        # prints "Auto-merging <path>" to STDOUT for any path that needs a
        # real content-level merge (which every db/**/*.jsonl row-merge does,
        # per .gitattributes' merge=kcsync-rows). This function's own stdout
        # is its return channel -- the caller does
        # `result="$(merge_remote_refs)"` and then
        # `read -r merged conflicted incompatible <<< "$result"` -- and
        # `read` takes only the FIRST line of a multi-line value. A leaked
        # "Auto-merging db/memory/episodic_memories.jsonl" line arrives
        # before the real final `echo "$merged $conflicted $incompatible"`,
        # so `read` bound merged/conflicted/incompatible to words of the
        # leaked line instead, and the later `[ "$conflicted" -gt 0 ]` blew
        # up with "integer expected". That comparison sitting inside an
        # `if` meant the bad value was merely swallowed as "false" here --
        # but the exact same corruption would have hidden a REAL conflict
        # (conflicted=1 replaced by non-numeric garbage that also fails
        # `-gt 0`), silently skipping report_conflicts()/show_unresolved()
        # and proceeding to pack a half-merged repo. Verified by direct
        # repro: two machines whose only overlap is an insert-only jsonl
        # table (no real conflict) reproduced the "integer expected" text
        # on 9 of 10 runs even though nothing here was otherwise flaky.
        if KCSYNC_STRATEGY="$STRATEGY" \
           KCSYNC_REPO="$SYNC_REPO" \
           KCSYNC_CONFLICT_LOG="$CONFLICT_LOG" \
           git_repo merge -q --no-edit ${extra[@]+"${extra[@]}"} \
                -m "sync: merge $machine" "$ref" > /dev/null 2>&1; then
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

    # Stamp AFTER the pack, so every database file it just wrote is older than
    # the marker and the next run does not mistake our own writes for KiroCrew
    # being busy. Best-effort: a sync that worked must not fail on a marker.
    mkdir -p "$SYNC_ROOT" 2>/dev/null || true
    touch "$PACK_MARKER" 2>/dev/null || true
}

# bundle_name() is <machine-id>.bundle -- no scope component. Changing that
# would be a wire-format break (every existing deployment's bundles are named
# this way; see ADR 0002's wire-format caveat), so instead this is a guard: if
# the SAME machine has already published a bundle under a DIFFERENT scope at
# this same backend location, publishing here would silently overwrite it --
# whichever scope publishes second wins the filename, and the other scope's
# state is simply gone from the remote, even though it is untouched locally.
#
# fetch_bundles() skips this machine's own bundle on purpose (there is
# nothing to merge from yourself), so the ordinary per-remote-machine compat
# check in check_remote_compat() never looks at it -- that function is what
# owns "read .kcsync-scope out of a fetched ref" (git show <ref>:.kcsync-scope
# | tr -d whitespace, legacy/absent meaning personal), and this reuses that
# exact mechanism pointed at our own bundle rather than inventing a second way
# to read the same file.
check_own_scope_collision() {
    local temp_dir="$1"
    local own_bundle="$temp_dir/bundles/$(bundle_name)"
    [ -f "$own_bundle" ] || return 0

    # A private ref namespace, deliberately NOT refs/remotes/* -- that is what
    # merge_remote_refs() scans for machines to merge, and this is not one: it
    # is our own previously-published bundle, fetched only to read one file
    # out of its tree. Keeping it out of refs/remotes/ means a ref this
    # function fails to clean up (a crash between fetch and delete, say) is
    # simply invisible to every other function here, rather than being merged
    # on the next sync as a phantom machine named after this scratch ref.
    local scratch_ref="refs/kcsync-self/$SYNC_BRANCH"
    git_repo update-ref -d "$scratch_ref" > /dev/null 2>&1 || true

    if ! git_repo bundle verify "$own_bundle" > /dev/null 2>&1; then
        return 0   # unreadable; not this guard's problem to diagnose
    fi
    # Force-fetch (leading +): this ref never has real history of its own to
    # fast-forward from, only whatever a previous, possibly-interrupted run of
    # this same check left behind.
    if ! git_repo fetch -q "$own_bundle" \
        "+refs/heads/$SYNC_BRANCH:$scratch_ref" 2>/dev/null; then
        return 0
    fi

    local remote_scope
    remote_scope="$(git_repo show "$scratch_ref:.kcsync-scope" 2>/dev/null \
        | tr -d '[:space:]')"
    git_repo update-ref -d "$scratch_ref" > /dev/null 2>&1 || true
    # Legacy bundles predate the scope marker; absent means personal -- same
    # fallback check_remote_compat() uses for every other machine's bundle.
    [ -n "$remote_scope" ] || remote_scope="personal"

    if [ "$remote_scope" != "$SYNC_SCOPE" ]; then
        log_error "Remote already holds a bundle for this machine ($(get_machine_id))"
        log_error "published at scope '$remote_scope'; this run is scope '$SYNC_SCOPE'."
        log_info ""
        log_info "Bundle filenames carry no scope, so publishing now would overwrite that"
        log_info "machine's '$remote_scope' bundle with this '$SYNC_SCOPE' one and corrupt"
        log_info "both. Give each scope its own backend location -- a different S3_PREFIX,"
        log_info "LOCAL_SYNC_DIR, or remote directory -- through a separate config file"
        log_info "selected with KIROCREW_SYNC_CONFIG, e.g.:"
        log_info "  KIROCREW_SYNC_CONFIG=~/.kiro/crew/team-config.sh $0 sync --team"
        return 1
    fi
    return 0
}

publish_bundle() {
    local temp_dir="$1"
    check_own_scope_collision "$temp_dir" || exit 1
    mkdir -p "$temp_dir/bundles"
    git_repo bundle create "$temp_dir/bundles/$(bundle_name)" "$SYNC_BRANCH" \
        > /dev/null 2>&1
    log_info "Publishing to $BACKEND..."
    backend_push "$temp_dir"
}

# --------------------------------------------------------------------------

cmd_sync() {
    acquire_sync_lock
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
    acquire_sync_lock
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
    acquire_sync_lock
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
    # No separate acquire_sync_lock() call: this runs cmd_sync() in the same
    # process, which takes the lock itself. The lock is not reentrant, so
    # acquiring it here too would make cmd_sync()'s own acquire_sync_lock()
    # find our own PID already holding it, `kill -0` it as alive, and fail
    # with "another sync is already running" against ourselves.
    cmd_sync
}

# --------------------------------------------------------------------------
# export / import -- the seeding primitive named as a follow-up in ADR 0002
# ("Snapshot + restore as a KiroCrew subcommand"). export packages the
# current scope's synced state as one file; import seeds a machine from one.
#
# Deliberately NOT a merge. A merge needs two live states *and* their common
# ancestor; an archive is a single snapshot with no ancestor against this
# machine's own state, so there is nothing for "merge" to mean here -- see
# the ADR's "Snapshot + restore" section for the full argument. That is what
# `sync` is for, once both machines share real history. import only ever
# replaces: either there is nothing local yet to conflict with, or --force
# says to discard what is.
# --------------------------------------------------------------------------

cmd_export() {
    if [ -z "$EXPORT_OUTPUT" ]; then
        log_error "export requires -o/--output <path>"
        log_info "Example: $0 export -o snap.tar.gz"
        exit 1
    fi

    acquire_sync_lock
    require_python
    require_git
    ensure_repo

    log_info "Unpacking local state..."
    kcsync_scoped unpack --kirocrew-dir "$KIROCREW_DIR" --repo "$SYNC_REPO"
    if commit_local_state "export: local state from $(get_machine_id)"; then
        log_success "Recorded local changes"
    else
        log_info "No local changes since last sync"
    fi

    if ! repo_has_commit; then
        log_error "Nothing to export yet."
        exit 1
    fi

    make_temp_dir
    local build="$TEMP_DIR/build"
    mkdir -p "$build"

    log_info "Bundling sync history..."
    # A full bundle of one ref with no negative boundary carries the whole
    # reachable history, not just its tip -- that ancestry is the point: an
    # imported machine gets a real merge base and joins the existing pair
    # cleanly on its next ordinary sync, which a bare state tarball cannot
    # offer (see ADR 0002's "Snapshot + restore" section). blob/ needs no
    # separate handling to make that true: it is a normal tracked directory
    # under $SYNC_REPO (ensure_repo's .gitattributes marks it `binary`, not
    # "untracked"), so every embedding it holds travels inside this same
    # bundle exactly like db/ and files/ do -- packaging it a second time
    # would only duplicate bytes already inside the bundle's packed objects.
    git_repo bundle create "$build/repo.bundle" "$SYNC_BRANCH" > /dev/null 2>&1

    log_info "Writing manifest..."
    # Secrets: this archive is the unpack output plus a bundle of it, and
    # unpack_files() already strips every credential-shaped JSON leaf
    # (files.py's SECRET_KEY_RE) before anything is written into $SYNC_REPO --
    # the same redaction `sync`/`push` rely on before publishing a bundle to
    # the backend. There is nothing export-specific to strip; tests/test_seed.sh
    # asserts the archive this produces does not contain a known secret value.
    kcsync_scoped seed-manifest --kirocrew-dir "$KIROCREW_DIR" \
        --machine-id "$(get_machine_id)" > "$build/manifest.json"

    tar -C "$build" -czf "$TEMP_DIR/seed.tar.gz" manifest.json repo.bundle
    mkdir -p "$(dirname "$EXPORT_OUTPUT")"
    mv "$TEMP_DIR/seed.tar.gz" "$EXPORT_OUTPUT"

    log_success "Exported to $EXPORT_OUTPUT"
    log_info "Scope: $SYNC_SCOPE   Machine: $(get_machine_id)"
}

cmd_import() {
    if [ -z "$IMPORT_ARCHIVE" ]; then
        log_error "import requires an archive path"
        log_info "Example: $0 import snap.tar.gz"
        exit 1
    fi
    if [ ! -f "$IMPORT_ARCHIVE" ]; then
        log_error "Archive not found: $IMPORT_ARCHIVE"
        exit 1
    fi

    acquire_sync_lock
    require_python
    require_git

    make_temp_dir
    local extract="$TEMP_DIR/extract"
    mkdir -p "$extract"
    if ! tar -xzf "$IMPORT_ARCHIVE" -C "$extract" 2>/dev/null; then
        log_error "Could not read archive: $IMPORT_ARCHIVE"
        exit 1
    fi
    if [ ! -f "$extract/manifest.json" ] || [ ! -f "$extract/repo.bundle" ]; then
        log_error "Not a kirocrew-sync seed archive (expected manifest.json and repo.bundle)"
        exit 1
    fi

    log_info "Checking archive against local state..."
    # Scope and embedding-space mismatches are refused outright, with no
    # --force override -- unlike sync's per-machine quarantine, where
    # skipping one incompatible machine is harmless because every other
    # machine still merges, import has exactly one input. Forcing past a
    # wrong scope or a foreign embedding model would seed this machine's
    # only copy of the data with it.
    if ! kcsync_scoped seed-check --kirocrew-dir "$KIROCREW_DIR" \
            --manifest "$extract/manifest.json"; then
        log_error "Refusing to import."
        exit 1
    fi
    log_success "Archive matches this machine's scope and embedding space"

    local existing=false
    local summary=""
    if [ -d "$SYNC_REPO/.git" ] && repo_has_commit; then
        existing=true
    fi
    if summary="$(kcsync_scoped seed-has-data --kirocrew-dir "$KIROCREW_DIR" 2>/dev/null)"; then
        existing=true
    fi

    if $existing && ! $FORCE; then
        log_error "This machine already has local state for scope '$SYNC_SCOPE'."
        echo
        [ -d "$SYNC_REPO/.git" ] && log_info "  Sync repo: $SYNC_REPO"
        [ -n "$summary" ] && echo "$summary"
        echo
        log_info "--force will:"
        log_info "  - discard this scope's local sync history ($SYNC_REPO) and replace it"
        log_info "    with the archive's -- import seeds, it does not merge (there is no"
        log_info "    common ancestor to merge against here; that is what 'sync' is for)"
        log_info "  - make every synced database table match the archive exactly: rows"
        log_info "    that only exist on this machine are DELETED, rows in the archive"
        log_info "    are inserted or overwrite the local ones"
        log_info "  - overwrite every synced file with the archive's copy, except"
        log_info "    credential-shaped JSON fields, which are kept from this machine"
        log_info "    (re-grafted, exactly like every other pack)"
        log_info "  - back up both databases first, exactly like any other pack"
        echo
        log_info "Re-run with --force to proceed."
        exit 1
    fi

    log_info "Materializing sync repo from the archive..."
    rm -rf "$SYNC_REPO"
    # A bare `git init`, not ensure_repo() -- ensure_repo() also writes
    # .gitattributes straight into the working tree, untracked, and the
    # checkout below would then refuse: git will not clobber an untracked
    # file that the ref being checked out also carries. Run ensure_repo()
    # afterwards instead, once the checkout has populated the tree; being
    # idempotent, it configures merge drivers and refreshes .gitattributes
    # to this script's own version, exactly as it does for every other repo
    # it opens, checked-out-from-a-bundle or not.
    git init -q -b "$SYNC_BRANCH" "$SYNC_REPO"
    git_repo fetch -q "$extract/repo.bundle" \
        "refs/heads/$SYNC_BRANCH:refs/remotes/seed-import/$SYNC_BRANCH"
    git_repo checkout -q -B "$SYNC_BRANCH" "refs/remotes/seed-import/$SYNC_BRANCH"
    git_repo update-ref -d "refs/remotes/seed-import/$SYNC_BRANCH" > /dev/null 2>&1 || true
    ensure_repo

    apply_to_kirocrew
    log_success "Seeded local KiroCrew state from $IMPORT_ARCHIVE"

    if $DRY_RUN; then
        log_success "Dry run complete. Nothing was published."
        return 0
    fi

    make_temp_dir
    backend_pull "$TEMP_DIR" 2>/dev/null || true
    publish_bundle "$TEMP_DIR"
    log_success "Import complete"
    log_info "Run '$0 sync' from now on to merge further changes with the rest of the pair."
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
  export      Archive this scope's synced state, to seed another machine
  import      Seed this machine's local state from an export archive
  daemon      Run background sync (polls for changes, auto-syncs)
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
                   gate fails (sync/pull/resume, applies to every machine at
                   once -- not the way to work around a single lagging
                   machine, which sync already skips and carries on). For
                   import, skip the existing-local-state check and replace
                   it with the archive.
  -o, --output <path>   export: where to write the archive (required)

Exit codes:
  0   Success
  1   Sync stopped; your data was not changed (includes: another sync,
      push, resume, or pull is already running for this scope)
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
  $0 export -o snap.tar.gz
  $0 import snap.tar.gz
  $0 import snap.tar.gz --force

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

export/import seed a new machine from an existing one's state -- a real git
history, not just a snapshot, so the seeded machine gets an actual merge
base and joins future syncs cleanly instead of starting from nothing. There
is no merge mode for import: a merge needs two live states *and* their
common ancestor, and a standalone archive has no ancestor against this
machine's own data -- that is exactly what "sync" provides once both
machines share real history. import only ever replaces, and only with
--force when there is something local it would replace.
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
        -o|--output)
            EXPORT_OUTPUT="${2:-}"; shift 2 ;;
        --output=*)
            EXPORT_OUTPUT="${1#*=}"; shift ;;
        --mode)
            IMPORT_MODE="${2:-}"; shift 2 ;;
        --mode=*)
            IMPORT_MODE="${1#*=}"; shift ;;
        -*)
            log_error "Unknown option: $1"; show_help; exit 1 ;;
        *)
            # The one positional argument this script accepts at all: the
            # archive path for `import`. Everything else stays flag-only, so
            # a stray bare word anywhere else still errors exactly as before.
            if [ "$COMMAND" = "import" ] && [ -z "$IMPORT_ARCHIVE" ]; then
                IMPORT_ARCHIVE="$1"; shift
            else
                log_error "Unknown option: $1"; show_help; exit 1
            fi
            ;;
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

# There is deliberately no merge mode for import (see ADR 0002's "Snapshot +
# restore as a KiroCrew subcommand"): a merge needs two live states and their
# common ancestor, and a standalone archive has none against this machine's
# own data. "replace" is the only real mode and is also the default, so it is
# accepted as a no-op rather than required; anything merge-shaped gets this
# pointer instead of a generic error, and anything else is just rejected.
if [ -n "$IMPORT_MODE" ] && [ "$IMPORT_MODE" != "replace" ]; then
    case "$(echo "$IMPORT_MODE" | tr '[:upper:]' '[:lower:]')" in
        *merge*)
            log_error "There is no 'merge' mode for import."
            log_info "A merge needs two live states and their common ancestor; a"
            log_info "standalone archive has no ancestor against this machine's own"
            log_info "data, so there is nothing for 'merge' to mean here. That is"
            log_info "what 'sync' does, with a real base -- see"
            log_info "docs/adr/0002-three-way-sync-via-unpacked-git-repo.md"
            ;;
        *)
            log_error "Unknown import mode: $IMPORT_MODE (only 'replace' exists, and it is the default)"
            ;;
    esac
    exit 1
fi

case "$COMMAND" in
    init)    cmd_init ;;
    sync)    cmd_sync ;;
    push)    cmd_push ;;
    pull)    cmd_pull ;;
    resume)  cmd_resume ;;
    export)  cmd_export ;;
    import)  cmd_import ;;
    daemon)  cmd_daemon ;;
    status)  cmd_status ;;
    doctor)  cmd_doctor ;;
    paths)   cmd_paths ;;
    help|--help|-h) show_help ;;
    *)
        log_error "Unknown command: $COMMAND"
        show_help
        exit 1 ;;
esac
