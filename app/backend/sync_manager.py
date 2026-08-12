"""
Sync manager - wrapper around kirocrew-sync.sh bash script.

Executes sync, parses output, and returns structured results. Also owns the
app's view of daemon configuration/state (backed by the daemon_state table)
and the daemon lifecycle (start/stop/restart), by driving the same lock file
lib/daemon.sh uses -- this module never reimplements sync or merge logic,
only shells out to the bash engine and reads what it writes to disk.
"""

import os
import re
import signal
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import artifacts
from .conflicts import ConflictManager
from .database import Database
from .models import Conflict, SyncResult, SyncStatus
from .quarantine import QuarantineManager


class SyncEngineUnavailable(RuntimeError):
    """
    Raised when an operation needs kirocrew-sync.sh but it is not installed
    at the resolved location. Callers (server.py) turn this into a clean
    HTTP 503 rather than letting it become an unhandled 500 traceback, and
    it never prevents the app from starting up -- see SyncManager.__init__.
    """


def _resolve_kirocrew_dir(sync_dir: Path) -> Path:
    """
    Resolve KIROCREW_DIR the same way kirocrew-sync.sh does: an explicit
    environment variable wins, then config.sh's own export, then the
    ~/.kiro/crew default.
    """
    env_value = os.environ.get("KIROCREW_DIR")
    if env_value:
        return Path(env_value)

    config_file = sync_dir / "config.sh"
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    match = re.match(
                        r'^\s*(?:export\s+)?KIROCREW_DIR\s*=\s*["\']?([^"\'\n]*)', line
                    )
                    if match:
                        value = match.group(1).strip()
                        if value:
                            # config.sh writes $HOME/.kiro/crew; the shell
                            # never runs here, so expand it ourselves.
                            value = value.replace("$HOME", str(Path.home()))
                            value = os.path.expandvars(value)
                            return Path(value)
        except OSError:
            pass

    return Path.home() / ".kiro" / "crew"


# kirocrew-sync.sh's log_error()/log_warn()/etc. (lines 32-35) always emit
# ANSI colour codes -- there is no TTY check -- so captured output has to be
# stripped before matching on the "✗ "/"⚠ " prefixes those helpers use.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class SyncManager:
    """Manages sync operations via the bash script, and the app's own daemon state."""

    def __init__(
        self,
        sync_dir: Optional[Path] = None,
        kirocrew_dir: Optional[Path] = None,
        db_path: Optional[Path] = None,
    ):
        # Resolution is lazy and never raises: a machine without the sync
        # engine installed still gets a working (degraded) app. Individual
        # operations that truly need the script raise SyncEngineUnavailable,
        # which server.py maps to a 503.
        if sync_dir is not None:
            self.sync_dir = Path(sync_dir)
        else:
            env_sync_dir = os.environ.get("KIROCREW_SYNC_DIR")
            self.sync_dir = (
                Path(env_sync_dir) if env_sync_dir
                else Path.home() / ".kiro/crew/workspace/kirocrew-sync"
            )
        self.script = self.sync_dir / "kirocrew-sync.sh"

        self.kirocrew_dir = (
            Path(kirocrew_dir) if kirocrew_dir is not None
            else _resolve_kirocrew_dir(self.sync_dir)
        )
        self.sync_root = self.kirocrew_dir / ".sync"

        # PIDs this instance itself launched via start_daemon(). Scopes
        # os.waitpid() in _pid_alive() to processes we actually forked --
        # see that method's docstring for why calling it on an arbitrary
        # PID from the lock file is unsafe.
        self._daemon_child_pids: set = set()

        self.db = Database(db_path)
        # Idempotent (CREATE TABLE IF NOT EXISTS / INSERT OR IGNORE): safe to
        # call regardless of whether HistoryManager has already done this on
        # the same underlying file. Without it, daemon_state reads would
        # fail with "no such table" if SyncManager is used before any other
        # manager happens to initialize the schema.
        self.db.initialize()

    @property
    def available(self) -> bool:
        """Whether the bash sync engine is installed at the resolved location."""
        return self.script.exists()

    def _require_script(self) -> None:
        if not self.script.exists():
            raise SyncEngineUnavailable(
                f"kirocrew-sync.sh not found at {self.script}. "
                "Set KIROCREW_SYNC_DIR to its install location, or install "
                "it from: https://github.com/pawelgradziel/kirocrew-sync"
            )

    # ------------------------------------------------------------------
    # Running sync
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        """Check if sync is currently running."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "kirocrew-sync.sh sync"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def run_sync(
        self,
        strategy: str = "auto",
        team: bool = False,
        dry_run: bool = False,
        timeout: int = 300
    ) -> SyncResult:
        """
        Run sync and parse results.

        Args:
            strategy: Conflict resolution strategy (auto, local-wins, remote-wins, manual)
            team: Use team scope instead of personal
            dry_run: Run without writing changes
            timeout: Command timeout in seconds

        Returns:
            SyncResult with parsed output

        Raises:
            SyncEngineUnavailable: kirocrew-sync.sh is not installed
            subprocess.TimeoutExpired: If sync takes longer than timeout
        """
        self._require_script()
        start_time = time.time()

        cmd = [str(self.script), "sync", "--strategy", strategy]
        if team:
            cmd.append("--team")
        if dry_run:
            cmd.append("--dry-run")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(self.sync_dir)
        )

        duration_ms = int((time.time() - start_time) * 1000)

        output = result.stdout + result.stderr
        parsed = self._parse_output(output)
        conflicts_count, quarantine_count = self._artifact_counts(team)

        return SyncResult(
            exit_code=result.returncode,
            duration_ms=duration_ms,
            scope="team" if team else "personal",
            strategy=strategy,
            dry_run=dry_run,
            changes_detected=parsed["changes_detected"],
            rows_merged=parsed["rows_merged"],
            conflicts_count=conflicts_count,
            quarantine_count=quarantine_count,
            output=output,
            error=self._extract_error(output, result.returncode)
        )

    def _parse_output(self, output: str) -> dict:
        """
        Derive changes_detected/rows_merged from what cmd_sync() actually
        prints (kirocrew-sync.sh:409-483). Every log_* helper (lines 32-35)
        writes to stdout with ANSI colour codes and no TTY check, so this
        strips those before matching.

        changes_detected:
            True iff either "something happened" line is present:
              - "Recorded local changes"     (cmd_sync ~line 425: local
                diff was committed)
              - "Merged N remote machine(s)" with N > 0 (cmd_sync ~line
                450, via merge_remote_refs() ~line 304)
            False otherwise -- including "No local changes since last
            sync" + "No remote machines found", the genuinely-idle case.
            The exit code cannot substitute for this: exit 0 also covers a
            run where nothing at all changed, so exit_code in (0, 3) was
            never a valid signal here (this was the original bug).

        rows_merged:
            Summed from every "packed <db>: N rows written, M deleted"
            line (lib/kcsync/cli.py cmd_pack(), ~line 129) -- one per
            KiroCrew database (memory.db, knowledge.db; see
            lib/kcsync/policy.py DATABASES). This is the *only* place the
            engine ever reports a row count; cmd_sync() itself never
            prints one, and the previous implementation's `(\\d+)\\s+rows?`
            regex was actually matching the digit out of "N row
            conflict(s) resolved automatically" (report_conflicts(),
            kirocrew-sync.sh:364) -- the conflict count, not a row count.
            cli.py's log() writes to stderr, but run_sync() combines
            stdout+stderr before calling this, so the text is still here.
            If pack() never ran (sync failed before apply_to_kirocrew() --
            unresolved git conflicts, a blocked gate, KiroCrew still
            running) there is no line to sum and this is 0. That 0 is the
            type system's floor (SyncResult.rows_merged is a plain,
            non-Optional int in models.py) -- it is NOT a claim that zero
            rows were verified written; see run_sync()'s error handling
            for how a failed run is otherwise distinguished.

        conflicts_count/quarantine_count are deliberately NOT computed
        here -- see _artifact_counts(), which counts conflicts.jsonl /
        quarantine.txt directly rather than regexing prose for numbers
        that were sometimes outright wrong (the conflicts regex above) or
        just needlessly indirect (the engine's own quarantine count is
        exactly len(quarantine.txt) already).
        """
        clean = _strip_ansi(output)

        changes_detected = (
            "Recorded local changes" in clean
            or bool(re.search(r"Merged\s+[1-9]\d*\s+remote machine", clean))
        )

        rows_merged = sum(
            int(n) for n in
            re.findall(r"packed\s+\S+:\s*(\d+)\s+rows written", clean)
        )

        return {
            "changes_detected": changes_detected,
            "rows_merged": rows_merged,
        }

    def _artifact_counts(self, team: bool) -> Tuple[int, int]:
        """
        (conflicts_count, quarantine_count) for the run that just finished,
        read straight from this scope's conflicts.jsonl / quarantine.txt --
        the same files cmd_sync() truncates at the start of every run
        (kirocrew-sync.sh:412-413) and ingest_artifacts() parses moments
        later via the same artifacts.read_conflicts()/read_quarantine().

        This replaces two prose regexes: one that was flatly broken (the
        "conflicts_count" pattern `(\\d+)\\s+conflict` could never match
        "N row conflict(s)" -- "row" sits between the digit and the word
        "conflict") and one that was merely indirect (the quarantine
        count is exactly len(quarantine.txt); the engine's own "N
        machine(s) quarantined" text says the same number). Counting the
        artifact files themselves is exact by construction and matches
        what ingest_artifacts() will persist to the database right after.
        """
        paths = artifacts.scope_paths(self.sync_root, team)
        conflicts_count = len(artifacts.read_conflicts(paths["conflicts"]))
        quarantine_count = len(artifacts.read_quarantine(paths["quarantine"]))
        return conflicts_count, quarantine_count

    def _extract_error(self, output: str, exit_code: int) -> Optional[str]:
        """
        The real failure text for a genuinely failed run, or None.

        Exit code 3 is success-with-quarantine (cmd_sync ~lines 479-483),
        not a failure, so it must never populate this field even though
        it is nonzero.

        Every failure path in kirocrew-sync.sh goes through log_error(),
        e.g. "Unresolved conflicts. Sync stopped before touching your
        data." (line 370), "Pack failed; KiroCrew data was left unchanged
        (or restored)." (line 393), "A previous sync left unresolved
        conflicts." (line 417). Like all four log_* helpers (lines 32-35)
        it writes to *stdout*, not stderr -- so `result.stderr if
        returncode != 0 else None` (the previous implementation) was
        reading a stream the engine never writes error text to, and the
        field came out empty for real failures. We instead pull every
        log_error() ("✗ ...") line out of the combined stdout+stderr text,
        stripping the ANSI colour codes log_error() always emits.

        Falls back to the raw combined output (a genuine crash -- Python
        traceback, `set -e` abort, a killed process -- can land outside
        any log_error() call) and only then to None, so a caller can
        apply its own "exit code N" fallback (see server.py's
        _send_sync_notifications) instead of this manufacturing text.
        """
        if exit_code in (0, 3):
            return None

        clean = _strip_ansi(output)
        error_lines = [
            line.strip() for line in clean.splitlines()
            if line.strip().startswith("✗")
        ]
        if error_lines:
            return "\n".join(error_lines)

        return clean.strip() or None

    # ------------------------------------------------------------------
    # Artifact ingestion (conflicts.jsonl / quarantine.txt -> database)
    # ------------------------------------------------------------------

    def ingest_artifacts(
        self,
        run_id: int,
        team: bool,
        conflict_mgr: ConflictManager,
        quarantine_mgr: QuarantineManager,
    ) -> Dict[str, list]:
        """
        Read this scope's conflicts.jsonl and quarantine.txt -- which
        cmd_sync() truncates at the start of every run, so they describe
        only the run that was just completed -- and persist them.

        Returns a dict describing what was ingested, so the caller can drive
        notifications:
            conflicts: List[artifacts.ConflictRecord]  (everything parsed)
            quarantine: List[str]                       (everything parsed)
            new_unresolved_conflicts: List[Conflict]     (freshly-inserted, unresolved)
            new_quarantine: List[str]                    (machines not already quarantined)
        """
        paths = artifacts.scope_paths(self.sync_root, team)
        machine_id = artifacts.get_local_machine_id(self.kirocrew_dir)

        conflict_records = artifacts.read_conflicts(paths["conflicts"])
        quarantine_machines = artifacts.read_quarantine(paths["quarantine"])
        previously_quarantined = self._quarantined_machine_names()

        new_unresolved: List[Conflict] = []
        for record in conflict_records:
            resolved, resolution = record.resolved_pair()
            conflict_id = conflict_mgr.record_conflict(
                run_id=run_id,
                machine=machine_id,
                table_name=record.table,
                row_id=record.key,
                local_value=record.path,
                remote_value=record.kind,
                resolved=resolved,
                resolution=resolution,
            )
            if not resolved:
                new_unresolved.append(Conflict(
                    id=conflict_id,
                    run_id=run_id,
                    machine=machine_id,
                    table_name=record.table,
                    row_id=record.key,
                    local_value=record.path,
                    remote_value=record.kind,
                    resolved=False,
                    resolution=None,
                    resolved_at=None,
                ))

        new_quarantine: List[str] = []
        for machine in quarantine_machines:
            if machine not in previously_quarantined:
                new_quarantine.append(machine)
            # quarantine.txt never explains *why* -- it is just machine ids
            # (see kirocrew-sync.sh merge_remote_refs()) -- so the reason is
            # necessarily generic; "other" is the CHECK-constraint-safe
            # option since we cannot tell version/embedding/scope mismatch
            # apart from this file alone.
            quarantine_mgr.add_quarantine(
                machine=machine,
                reason="other",
                details=(
                    "Quarantined by the bash sync engine (incompatible "
                    "version, embedding model, or scope). The specific "
                    "cause is not recorded in quarantine.txt; run "
                    "'kirocrew-sync.sh status' on this machine for detail."
                ),
            )

        return {
            "conflicts": conflict_records,
            "quarantine": quarantine_machines,
            "new_unresolved_conflicts": new_unresolved,
            "new_quarantine": new_quarantine,
        }

    def _quarantined_machine_names(self) -> set:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT machine FROM quarantine WHERE cleared_at IS NULL"
            ).fetchall()
        return {row["machine"] for row in rows}

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_active_machine_count(self, team: bool = False) -> int:
        """
        Count distinct remote machines (git refs under refs/remotes) in this
        scope's sync repo, excluding machines currently quarantined in the
        app's database. Returns 0 when the repo does not exist yet.
        """
        paths = artifacts.scope_paths(self.sync_root, team)
        quarantined = self._quarantined_machine_names()
        return artifacts.count_active_machines(paths["repo"], quarantined)

    def get_next_sync(self) -> Optional[datetime]:
        """
        Always None: this app has no way to know when the daemon will next
        run.

        lib/daemon.sh drives its own adaptive polling loop with hardcoded
        INTERVAL_IDLE=300 / INTERVAL_ACTIVE=30 / INTERVAL_BACKOFF=600
        (lib/daemon.sh:26-28), chosen fresh each cycle in daemon_cycle()
        (lib/daemon.sh:113-182) from what that cycle just observed --
        remote changes, local changes, whether KiroCrew is busy, whether
        the last sync succeeded/quarantined/failed. It never reads this
        app's daemon_state table (grep-verified across lib/daemon.sh), so
        the "interval" this app exposes via get_daemon_config()/
        update_daemon_config() (60-900s, stored in daemon_state) does not
        govern the daemon's actual schedule at all -- it is this app's own
        setting, not one lib/daemon.sh consumes.

        `last_run + interval` therefore was never a real prediction, just
        an arithmetic result computed from two numbers with no causal
        connection to what the daemon does next -- presenting a guess as
        fact. Returning None here is the honest answer; a real ETA would
        require the daemon to record its own next-wake time to
        daemon_state, which lib/daemon.sh does not currently do.
        """
        return None

    def _daemon_state_datetime(self, key: str) -> Optional[datetime]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT value FROM daemon_state WHERE key = ?", (key,)
            ).fetchone()
        raw = row["value"] if row else ""
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def get_status(self, team: bool = False) -> SyncStatus:
        """Best-effort standalone status snapshot using only what this manager owns."""
        scope = "team" if team else "personal"
        machines_quarantined = len(self._quarantined_machine_names())
        conflicts_pending = self._unresolved_conflict_count()

        if self.is_running():
            state = "syncing"
        elif conflicts_pending > 0:
            state = "conflict"
        elif machines_quarantined > 0:
            state = "quarantine"
        else:
            state = "idle"

        return SyncStatus(
            state=state,
            last_sync=self._daemon_state_datetime("last_run"),
            next_sync=self.get_next_sync(),
            scope=scope,
            machines_active=self.get_active_machine_count(team=team),
            machines_quarantined=machines_quarantined,
            conflicts_pending=conflicts_pending,
        )

    def _unresolved_conflict_count(self) -> int:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM conflicts WHERE resolved = 0"
            ).fetchone()
        return row["count"] if row else 0

    # ------------------------------------------------------------------
    # Daemon configuration (daemon_state table)
    # ------------------------------------------------------------------

    def get_daemon_config(self) -> dict:
        """Get daemon configuration from the daemon_state table."""
        with self.db.connect() as conn:
            rows = conn.execute("SELECT key, value FROM daemon_state").fetchall()
        state = {row["key"]: row["value"] for row in rows}
        try:
            interval = int(state.get("interval") or 300)
        except ValueError:
            interval = 300
        return {
            "enabled": state.get("enabled", "true") == "true",
            "scope": state.get("scope") or "personal",
            "interval": interval,
        }

    def update_daemon_config(
        self,
        enabled: Optional[bool] = None,
        scope: Optional[str] = None,
        interval: Optional[int] = None,
    ) -> dict:
        """Update daemon configuration in the daemon_state table."""
        if interval is not None and not (60 <= interval <= 900):
            raise ValueError("interval must be between 60 and 900 seconds")
        if scope is not None and scope not in ("personal", "team"):
            raise ValueError("scope must be 'personal' or 'team'")

        current = self.get_daemon_config()
        if enabled is not None:
            current["enabled"] = enabled
        if scope is not None:
            current["scope"] = scope
        if interval is not None:
            current["interval"] = interval

        self._write_daemon_state({
            "enabled": "true" if current["enabled"] else "false",
            "scope": current["scope"],
            "interval": str(current["interval"]),
        })
        return current

    def record_daemon_run(self, timestamp: Optional[datetime] = None) -> None:
        """Record last_run/next_run in daemon_state after a completed sync run."""
        timestamp = timestamp or datetime.now()
        interval = self.get_daemon_config()["interval"]
        next_run = timestamp + timedelta(seconds=interval)
        self._write_daemon_state({
            "last_run": timestamp.isoformat(),
            "next_run": next_run.isoformat(),
        })

    def _write_daemon_state(self, values: Dict[str, str]) -> None:
        now = datetime.now().isoformat()
        with self.db.connect() as conn:
            for key, value in values.items():
                conn.execute(
                    """
                    INSERT INTO daemon_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                    updated_at = excluded.updated_at
                    """,
                    (key, value, now),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Daemon lifecycle (lib/daemon.sh's lock file at $SYNC_ROOT/daemon.lock)
    # ------------------------------------------------------------------
    #
    # lib/daemon.sh acquire_daemon_lock()/release_daemon_lock() (~lines 38-58)
    # write the daemon's own PID as the sole content of $SYNC_ROOT/daemon.lock
    # (`echo $$ > "$DAEMON_LOCK"`) and remove it on exit via a trap. That lock
    # is NOT scope-specific -- $SYNC_ROOT = $KIROCREW_DIR/.sync regardless of
    # --team -- so only one daemon (personal or team) can run at a time per
    # machine. We drive the same file rather than inventing a pidfile path.

    def _daemon_lock_path(self) -> Path:
        return self.sync_root / "daemon.lock"

    def _read_daemon_pid(self) -> Optional[int]:
        """
        PID from the lock file, or None if absent/unreadable/stale/invalid/
        unconfirmed.

        This is the single choke point every daemon-lifecycle method
        (daemon_status, start_daemon, stop_daemon, restart_daemon) reads
        the PID through, so every safety check lives here once:

        - `pid <= 0` is rejected outright. A corrupt lock file can contain
          0 or a negative number; os.kill(0, sig) targets this whole
          process's *process group* (i.e. this server), and
          os.kill(-1, sig) targets every process the user can signal.
          Verified: a lock file containing "0" or "-1" was returned
          verbatim by the old implementation, which only ever checked
          `kill(pid, 0)` for liveness -- and kill(0, 0)/kill(-1, 0) both
          "succeed" without raising, since sig=0 is just a permission
          probe, not an identity check.
        - The PID must belong to an actual kirocrew-sync.sh daemon process
          (see _is_kirocrew_daemon_pid()), not merely be alive. Verified:
          writing an unrelated process's PID (a plain `sleep 300`) into
          daemon.lock made the old daemon_status() report "running" and
          stop_daemon() kill that unrelated process while returning
          success=True. When identity can't be confirmed, this refuses by
          returning None (as if no daemon were running) rather than
          treating "some process happens to be alive at this PID" as
          good enough to signal.
        """
        try:
            raw = self._daemon_lock_path().read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            pid = int(raw)
        except ValueError:
            return None
        if pid <= 0:
            return None
        if not self._pid_alive(pid):
            return None
        if not self._is_kirocrew_daemon_pid(pid):
            return None
        return pid

    def _is_kirocrew_daemon_pid(self, pid: int) -> bool:
        """
        Best-effort confirmation that `pid` is actually running
        kirocrew-sync.sh's `daemon` subcommand, not an unrelated process
        that happens to reuse this PID number (the lock file only ever
        records a bare integer -- there is nothing else to check it
        against). Refuses (False) whenever identity can't be read at all,
        since killing the wrong process is worse than failing to stop the
        right one.

        Reads /proc/<pid>/cmdline directly where available (Linux; exact,
        NUL-delimited argv), falling back to `ps -o args=` (portable --
        works on macOS too, and is also what kirocrew-sync.sh's own
        check_kirocrew_running() at line 161 uses for the same kind of
        check). Looks for the script's own basename plus the literal
        `daemon` argument, matching how start_daemon() invokes it
        (`[str(self.script), "daemon", ...]`) and how a shebang re-exec
        preserves both in argv either way.
        """
        cmdline = self._read_pid_cmdline(pid)
        if not cmdline:
            return False
        return self.script.name in cmdline and "daemon" in cmdline.split()

    def _read_pid_cmdline(self, pid: int) -> Optional[str]:
        proc_cmdline = Path(f"/proc/{pid}/cmdline")
        try:
            raw = proc_cmdline.read_bytes()
        except OSError:
            raw = b""
        if raw:
            parts = [p for p in raw.split(b"\0") if p]
            if parts:
                return " ".join(p.decode("utf-8", errors="replace") for p in parts)

        # /proc unavailable (non-Linux) or unreadable (permissions, pid
        # already gone) -- fall back to ps, the same portable mechanism
        # kirocrew-sync.sh itself uses.
        try:
            result = subprocess.run(
                ["ps", "-o", "args=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        args = result.stdout.strip()
        return args or None

    def daemon_status(self) -> dict:
        pid = self._read_daemon_pid()
        return {"running": pid is not None, "pid": pid}

    def start_daemon(self, team: Optional[bool] = None, wait: float = 2.0) -> Tuple[bool, str]:
        """Launch `kirocrew-sync.sh daemon` detached, and confirm it took the lock."""
        self._require_script()

        existing = self._read_daemon_pid()
        if existing is not None:
            return True, f"Daemon already running (PID {existing})"

        args = [str(self.script), "daemon"]
        if team:
            args.append("--team")

        self.sync_root.mkdir(parents=True, exist_ok=True)
        log_path = self.sync_root / "daemon.out.log"
        try:
            with open(log_path, "ab") as log_file:
                proc = subprocess.Popen(
                    args,
                    cwd=str(self.sync_dir),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
        except OSError as exc:
            return False, f"Failed to launch daemon: {exc}"

        # A shebang re-exec (#!/usr/bin/env bash) replaces the process image
        # in place rather than forking, so proc.pid stays valid as the
        # daemon's own PID ($$ in lib/daemon.sh) across the exec -- this is
        # the PID that ends up written to daemon.lock. Recorded so
        # _pid_alive() knows it is safe to waitpid() on this PID later.
        self._daemon_child_pids.add(proc.pid)

        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            pid = self._read_daemon_pid()
            if pid is not None:
                return True, f"Daemon started (PID {pid})"
            time.sleep(0.1)
        return False, (
            "Daemon process was launched but did not acquire its lock file "
            f"in time; check {log_path}"
        )

    def _pid_alive(self, pid: int) -> bool:
        # start_daemon() launches via subprocess.Popen(start_new_session=True):
        # that detaches the daemon's session/process group, but this process
        # remains its OS *parent*. So when it dies, it becomes a zombie --
        # still visible to kill(pid, 0) -- until reaped. Reap eagerly so a
        # killed daemon doesn't read as "still alive" forever.
        #
        # waitpid() is only called for PIDs in self._daemon_child_pids --
        # i.e. PIDs this instance itself got back from Popen() in
        # start_daemon(). Verified bug this replaced: the previous version
        # called os.waitpid(pid, WNOHANG) on *any* PID read out of the lock
        # file, with no check that it was actually our child. If some other
        # code path in this same process happens to have its own child at
        # that PID (e.g. a concurrent subprocess.run() call, or a daemon
        # discovered from daemon.lock that a *previous* instance of this
        # server -- not this one -- launched), blindly reaping it steals its
        # exit status from whatever real code is waiting on it. Restricting
        # to known-launched PIDs keeps this to processes we are actually
        # responsible for reaping; for any other PID we fall back to a
        # plain existence check only.
        if pid in self._daemon_child_pids:
            try:
                reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
                if reaped_pid == pid:
                    self._daemon_child_pids.discard(pid)
                    return False
            except ChildProcessError:
                # No longer our child (e.g. a previous call already reaped
                # it) -- fall through to the plain existence check below.
                self._daemon_child_pids.discard(pid)
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def stop_daemon(self, timeout: float = 5.0) -> Tuple[bool, str]:
        """
        SIGTERM the daemon PID from the lock file and confirm the process is
        actually gone -- not just that the lock file is gone -- escalating
        to SIGKILL if it does not exit in time.

        _read_daemon_pid() already refuses to hand back a PID unless it (a)
        is a positive integer, (b) is alive, and (c) is confirmed to
        actually be a kirocrew-sync.sh daemon process (see its docstring
        for the PID-reuse scenario this closes). This method still must
        not assume the PID stays valid/matching between that check and the
        kill() calls below (TOCTOU is inherent to PID-based signalling),
        but that risk window is now seconds, not indefinite.

        Why wait for the *process*, not just the lock file: lib/daemon.sh's
        `trap release_daemon_lock EXIT` / `trap 'release_daemon_lock; exit 0'
        INT TERM` (lines 197-198) do make TERM call release_daemon_lock()
        and exit -- but bash only runs a trap between commands, never while
        a foreground command is executing (POSIX signal-handling
        semantics). daemon_cycle()'s own `sleep "$next_interval"` runs in
        the foreground with next_interval as large as INTERVAL_BACKOFF=600s
        (lib/daemon.sh:26-28), so a SIGTERM arriving mid-sleep is not acted
        on until that sleep returns -- up to ten minutes later. Verified:
        against a real `sleep 300` standing in for that wait, this
        `timeout=5.0` graceful window elapses on essentially every call, so
        SIGKILL is reached in the common case, not just an edge case.
        Treating "lock released" as "stopped" would report success while
        the process (and possibly a sync it spawned) kept running, and
        would let start_daemon() launch a second daemon right on top of it.

        Residual risk this cannot close: SIGKILL takes down the daemon
        shell but not any `kirocrew-sync.sh sync` child it may have had
        in flight -- kirocrew-sync.sh has no lock of its own for that, only
        lib/daemon.sh's daemon.lock. That sync can keep running orphaned
        even though this reports the daemon stopped. The message on the
        force-kill path says so explicitly rather than claiming a clean
        stop.
        """
        pid = self._read_daemon_pid()
        if pid is None:
            return True, "Daemon was not running"

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            return False, f"Failed to signal daemon (PID {pid}): {exc}"

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._pid_alive(pid):
                return True, f"Daemon stopped (PID {pid})"
            time.sleep(0.2)

        # Still alive after the graceful window -- force it down rather than
        # reporting a false success or leaving an orphan for start_daemon()
        # to collide with.
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError as exc:
            return False, (
                f"Sent SIGTERM to PID {pid} but it did not stop, and SIGKILL "
                f"failed: {exc}"
            )

        kill_deadline = time.monotonic() + 2.0
        while time.monotonic() < kill_deadline:
            if not self._pid_alive(pid):
                # SIGKILL bypasses lib/daemon.sh's EXIT trap entirely, so
                # release_daemon_lock() never runs -- clean up the lock
                # ourselves (only if it still names the PID we just killed,
                # so a legitimately-started new daemon is never clobbered).
                self._clear_lock_if_pid_matches(pid)
                return True, (
                    f"PID {pid} did not exit after SIGTERM within {timeout}s "
                    "(its TERM trap can't run until any foreground `sleep` "
                    "in progress returns, per lib/daemon.sh's polling loop -- "
                    "see this method's docstring); force-killed it. If it had "
                    "a sync in progress, that child process may still be "
                    "running orphaned: kirocrew-sync.sh has no lock of its "
                    "own to prevent that."
                )
            time.sleep(0.1)
        return False, f"PID {pid} did not stop even after SIGKILL"

    def _clear_lock_if_pid_matches(self, pid: int) -> None:
        """Remove daemon.lock, but only if it still names `pid` -- guards
        against deleting a fresh lock some other (legitimately started)
        daemon wrote in the time since we last read this one."""
        lock_path = self._daemon_lock_path()
        try:
            raw = lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if raw == str(pid):
            try:
                lock_path.unlink()
            except OSError:
                pass

    def restart_daemon(self, team: Optional[bool] = None) -> Tuple[bool, str]:
        stopped, stop_message = self.stop_daemon()
        if not stopped:
            return False, f"Could not stop daemon before restart: {stop_message}"
        started, start_message = self.start_daemon(team=team)
        return started, start_message
