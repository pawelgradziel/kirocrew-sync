"""
Backend manager - handles backend configuration and testing.

Path resolution mirrors sync_manager.SyncManager: an explicit `sync_dir`
constructor argument wins, then the KIROCREW_SYNC_DIR environment variable,
then the ~/.kiro/crew/workspace/kirocrew-sync default -- the same three-tier
scheme, so both managers agree on where the sync engine (and its config.sh)
lives without KiroCrew ever needing a second override to keep in sync.

Like SyncManager, construction never raises -- a machine without the bash
sync engine installed still gets a working (degraded) app that can list
backends and read config defaults. Only operations that actually need to
write into config.sh (switch_backend, set_backend_config) raise
SyncEngineUnavailable, which server.py maps to a 503, exactly as it already
does for SyncManager.
"""

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import BackendInfo, BackendTestResult
from .sync_manager import SyncEngineUnavailable


@dataclass(frozen=True)
class BackendFieldSpec:
    """One backend setting: its config.sh/env key and its default, if any.

    `default is None` means the setting is genuinely required -- there is
    no value backends/*.sh could fall back to that would actually work
    (e.g. S3_BUCKET: the shipped backend script defaults it to the
    placeholder string "your-bucket-name", which is not a real bucket).
    """
    key: str
    default: Optional[str]

    @property
    def required(self) -> bool:
        return self.default is None


# The real config surface, read from config.sh.example and backends/*.sh:
#   gdrive: GDRIVE_REMOTE_NAME (default "kirocrew-gdrive"),
#           GDRIVE_SYNC_DIR (default "KiroCrew-Sync")
#   s3:     S3_BUCKET (required), S3_PREFIX (default "kirocrew-sync"),
#           AWS_PROFILE (default "default")
#   rsync:  RSYNC_HOST (required, user@hostname),
#           RSYNC_PATH (required, remote directory -- the shipped
#           "/path/to/kirocrew-sync" fallback is a placeholder, not a
#           usable default)
#   local:  LOCAL_SYNC_DIR (default "$HOME/Dropbox/KiroCrew-Sync")
BACKEND_FIELD_SPECS: Dict[str, List[BackendFieldSpec]] = {
    "gdrive": [
        BackendFieldSpec("GDRIVE_REMOTE_NAME", "kirocrew-gdrive"),
        BackendFieldSpec("GDRIVE_SYNC_DIR", "KiroCrew-Sync"),
    ],
    "s3": [
        BackendFieldSpec("S3_BUCKET", None),
        BackendFieldSpec("S3_PREFIX", "kirocrew-sync"),
        BackendFieldSpec("AWS_PROFILE", "default"),
    ],
    "rsync": [
        BackendFieldSpec("RSYNC_HOST", None),
        BackendFieldSpec("RSYNC_PATH", None),
    ],
    "local": [
        BackendFieldSpec("LOCAL_SYNC_DIR", "$HOME/Dropbox/KiroCrew-Sync"),
    ],
}

# The external command each backend shells out to, when knowable. None means
# the backend has no external dependency (local uses cp/rsync, but rsync is
# optional there -- backends/local.sh falls back to cp).
BACKEND_COMMANDS: Dict[str, Optional[str]] = {
    "gdrive": "rclone",
    "s3": "aws",
    "rsync": "rsync",
    "local": None,
}

# Key names that look credential-shaped -- redact their values in API
# responses even though, per config.sh.example, none of the fields this app
# actually manages hold real secrets today (rclone.conf, the AWS profile,
# and SSH keys hold the real credentials, outside config.sh entirely). This
# is a defensive backstop for future fields, not a claim that this app
# manages secrets now.
_CREDENTIAL_KEY_RE = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|API_KEY|ACCESS_KEY|PRIVATE_KEY)",
    re.IGNORECASE,
)

# Matches a plain `export KEY=value` (or `export KEY = value`) line. Only
# lines shaped like this are read as config values or considered candidates
# for in-place replacement; every other line (comments, blank lines, other
# shell code) is preserved verbatim.
_EXPORT_LINE_RE = re.compile(
    r'^[ \t]*export[ \t]+(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(?P<value>.*?)[ \t]*\r?\n?$'
)

# Valid shell identifier -- what we allow as a config.sh key at all.
_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

# Control characters (including \n, \r, \t, NUL) plus DEL. Rejected outright
# in any value written to config.sh -- see _validate_config_value.
_CONTROL_CHAR_RE = re.compile(r'[\x00-\x1f\x7f]')

# log_info/log_success/log_warn/log_error, byte-for-byte what
# kirocrew-sync.sh itself defines (see its lines ~26-35) before sourcing a
# backend script. backends/*.sh call these but do not define them -- they
# rely on kirocrew-sync.sh having done so already. Anything that sources a
# backend script standalone (as test_connection does) must define these
# first, or every log_* call inside backend_status fails with "command not
# found" and the script's exit status becomes that failure's, unrelated to
# whether the backend is actually reachable.
_LOG_HELPERS_SH = r"""
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}i${NC} $*"; }
log_success() { echo -e "${GREEN}+${NC} $*"; }
log_warn()    { echo -e "${YELLOW}!${NC} $*"; }
log_error()   { echo -e "${RED}x${NC} $*"; }
"""


def _redact(key: str, value: str) -> str:
    if value and _CREDENTIAL_KEY_RE.search(key):
        return "***REDACTED***"
    return value


def _parse_shell_value(raw: str) -> str:
    """Best-effort unquote of the right-hand side of `export KEY=<raw>`.

    Handles the two forms this app itself ever writes or encounters:
    single-quoted (what _write_config_values always produces) and
    double-quoted (what config.sh.example ships, e.g. SYNC_BACKEND="gdrive").
    Anything else is returned as-is. This does not attempt full shell
    parsing (no $VAR expansion, no command substitution) -- config.sh is
    meant to hold plain literal settings, and this app only needs to read
    back what it (or a human copying config.sh.example) would plausibly
    write there.
    """
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("'\\''", "'")
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    return raw


def _quote_shell_value(value: str) -> str:
    """Single-quote `value` for safe use as a bash literal, escaping any
    embedded single quote via the standard POSIX idiom: close the quote,
    emit an escaped literal quote, reopen the quote. Single quotes are the
    only bash quoting form with no special characters at all inside them --
    no `$`, backtick, `\\`, or `"` is ever interpreted -- so this alone
    prevents command substitution, variable expansion and escape sequences
    regardless of what the value contains (beyond the control-character
    rejection in _validate_config_value, which handles what quoting can't:
    a literal newline breaking the line structure itself).
    """
    return "'" + value.replace("'", "'\\''") + "'"


def _validate_config_value(key: str, value: str) -> None:
    if _CONTROL_CHAR_RE.search(value):
        raise ValueError(
            f"{key}: value contains control characters (including newlines), "
            "which is not allowed -- config.sh is sourced by bash on every "
            "sync run, and a newline could inject a second `export` line"
        )


class BackendManager:
    """Manages storage backend configuration."""

    def __init__(self, sync_dir: Optional[Path] = None):
        if sync_dir is not None:
            self.sync_dir = Path(sync_dir)
        else:
            env_sync_dir = os.environ.get("KIROCREW_SYNC_DIR")
            self.sync_dir = (
                Path(env_sync_dir) if env_sync_dir
                else Path.home() / ".kiro/crew/workspace/kirocrew-sync"
            )
        self.config_file = self.sync_dir / "config.sh"
        self.script = self.sync_dir / "kirocrew-sync.sh"

    @property
    def available(self) -> bool:
        """Whether the bash sync engine is installed at the resolved location."""
        return self.script.exists()

    def _require_engine(self) -> None:
        if not self.script.exists():
            raise SyncEngineUnavailable(
                f"kirocrew-sync.sh not found at {self.script}. "
                "Set KIROCREW_SYNC_DIR to its install location, or install "
                "it from: https://github.com/pawelgradziel/kirocrew-sync"
            )

    # ------------------------------------------------------------------
    # config.sh reading/writing
    # ------------------------------------------------------------------

    def _read_config_file_lines(self) -> List[str]:
        if not self.config_file.exists():
            return []
        with open(self.config_file, "r", encoding="utf-8") as f:
            return f.readlines()

    def _read_config_values(self) -> Dict[str, str]:
        """Parse every `export KEY=value` line in config.sh into a dict of
        raw (unquoted) string values. Missing file -> empty dict, which is
        the degraded-but-working state (everything falls back to env/defaults)."""
        values: Dict[str, str] = {}
        for line in self._read_config_file_lines():
            match = _EXPORT_LINE_RE.match(line)
            if match:
                values[match.group("key")] = _parse_shell_value(match.group("value"))
        return values

    def _write_config_values(self, updates: Dict[str, str]) -> None:
        """
        Persist `updates` into config.sh: replace an existing
        `export KEY=...` line for that key in place (preserving every other
        line, including comments, verbatim), or append a new
        `export KEY='value'` line at the end when the key is absent.
        Idempotent -- writing the same values twice produces byte-identical
        file content the second time, since the same line is matched and
        replaced with the same text rather than appended again.

        SECURITY: config.sh is `source`d by bash on every single sync run
        (kirocrew-sync.sh sources $CONFIG_FILE near the top), so any value
        written here is executed as shell code the next time sync runs. Two
        independent defenses apply, deliberately not relying on either
        alone:
          1. Every value is single-quoted via _quote_shell_value, with
             embedded single quotes escaped through the `'\\''` idiom. A
             single-quoted string is inert in bash -- no `;`, `$()`,
             backticks, `&&`, or `#` inside it is ever interpreted.
          2. Every value is validated via _validate_config_value first,
             which rejects control characters (including newlines) outright
             -- quoting protects against shell metacharacters within a
             single line, but a raw newline would still terminate the
             `export ...='...'` statement early and let a second line
             (e.g. `export EVIL=1`) execute as its own statement. Rejecting
             control characters closes that gap that quoting cannot.
        """
        for key in updates:
            if not _IDENT_RE.match(key):
                raise ValueError(f"invalid config key: {key!r}")
        for key, value in updates.items():
            _validate_config_value(key, value)

        self.sync_dir.mkdir(parents=True, exist_ok=True)
        lines = self._read_config_file_lines()
        remaining = dict(updates)

        for i, line in enumerate(lines):
            match = _EXPORT_LINE_RE.match(line)
            if match and match.group("key") in remaining:
                key = match.group("key")
                lines[i] = f"export {key}={_quote_shell_value(remaining.pop(key))}\n"

        for key, value in remaining.items():
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"export {key}={_quote_shell_value(value)}\n")

        with open(self.config_file, "w", encoding="utf-8") as f:
            f.writelines(lines)

    # ------------------------------------------------------------------
    # Current backend / listing
    # ------------------------------------------------------------------

    def get_current_backend(self) -> str:
        """Get currently configured backend from config.sh."""
        return self._read_config_values().get("SYNC_BACKEND") or "gdrive"

    def list_backends(self) -> List[BackendInfo]:
        """List all available backends."""
        current = self.get_current_backend()

        backends = [
            BackendInfo(
                name="gdrive",
                display_name="Google Drive",
                description="Sync via Google Drive using rclone",
                configured=self._check_backend_configured("gdrive"),
                active=current == "gdrive",
                requires_config=["rclone"]
            ),
            BackendInfo(
                name="s3",
                display_name="AWS S3",
                description="Sync via AWS S3 bucket",
                configured=self._check_backend_configured("s3"),
                active=current == "s3",
                requires_config=["awscli", "S3_BUCKET"]
            ),
            BackendInfo(
                name="rsync",
                display_name="Rsync",
                description="Sync via rsync to remote host",
                configured=self._check_backend_configured("rsync"),
                active=current == "rsync",
                requires_config=["rsync", "RSYNC_HOST"]
            ),
            BackendInfo(
                name="local",
                display_name="Local Directory",
                description="Sync to local folder (Dropbox, NAS mount, etc.)",
                configured=self._check_backend_configured("local"),
                active=current == "local",
                requires_config=["LOCAL_SYNC_DIR"]
            )
        ]

        return backends

    def _check_backend_configured(self, backend: str) -> bool:
        """
        A backend is "configured" when both of these hold:

          1. Every field with no usable default for that backend (i.e. a
             genuinely required setting -- S3_BUCKET, RSYNC_HOST,
             RSYNC_PATH; see BACKEND_FIELD_SPECS) has a non-empty effective
             value, from config.sh or the environment.
          2. The backend's required external command, if any (rclone, aws,
             rsync), is present on PATH.

        Backends whose settings are all defaulted (gdrive, local) are
        therefore "configured" out of the box, once their command (if any)
        is installed -- this mirrors what backends/*.sh itself does
        (`"${VAR:-default}"`: an unset optional variable is not an error).
        That is a different, weaker claim than "verified working": gdrive
        additionally needs a real rclone remote named GDRIVE_REMOTE_NAME to
        exist, and s3 needs the bucket to actually be reachable with
        AWS_PROFILE's credentials, neither of which is checked here.
        Confirming reachability is test_connection()'s job (POST
        /backends/test); "configured" only means "the required settings are
        present and the required tool is installed".
        """
        config_values = self._read_config_values()
        for spec in BACKEND_FIELD_SPECS.get(backend, []):
            if spec.required and not self._effective_value(spec, config_values)[0]:
                return False

        command = BACKEND_COMMANDS.get(backend)
        if command and shutil.which(command) is None:
            return False

        return True

    # ------------------------------------------------------------------
    # Effective settings (GET/PUT /backends/{name}/config)
    # ------------------------------------------------------------------

    def _effective_value(
        self, spec: BackendFieldSpec, config_values: Dict[str, str]
    ) -> "tuple[str, str]":
        """Returns (value, source). Precedence mirrors what actually happens
        when kirocrew-sync.sh runs: config.sh's `export KEY=...` (sourced
        after the pre-existing environment, so it overrides it) wins if
        present and non-empty, else this process's own environment (which
        stands in for whatever the shell's environment would have been) if
        set, else the backend script's own `${VAR:-default}` fallback."""
        config_value = config_values.get(spec.key, "")
        if config_value:
            return config_value, "config"
        env_value = os.environ.get(spec.key, "")
        if env_value:
            return env_value, "env"
        return (spec.default or ""), "default"

    def get_backend_settings(self, backend: str) -> List[dict]:
        """
        Effective settings for one backend: for each known field (see
        BACKEND_FIELD_SPECS), the value that would actually be in effect --
        config.sh beats the environment beats the backend script's default,
        see _effective_value -- plus whether that value is explicitly set
        or just defaulted, and whether the field is required.

        Values whose key name looks credential-shaped are redacted (see
        _redact) before being returned. None of the fields this app
        currently manages are real secrets -- per config.sh.example,
        credentials live in rclone's own config file, the named AWS
        profile, and SSH keys, none of which this app reads, stores, or
        reports -- but the redaction is applied unconditionally as a
        backstop rather than trusted to the current field list staying
        secret-free forever.

        Never raises for a missing config.sh (degrades to all-default),
        matching BackendManager's overall "never raise on read" posture.
        """
        config_values = self._read_config_values()
        results = []
        for spec in BACKEND_FIELD_SPECS.get(backend, []):
            value, source = self._effective_value(spec, config_values)
            results.append({
                "key": spec.key,
                "value": _redact(spec.key, value),
                "source": source,
                "is_set": source != "default",
                "required": spec.required,
            })
        return results

    def _validate_backend_config(
        self, backend: str, config: Dict[str, Any]
    ) -> Dict[str, str]:
        """Validate `config` against `backend`'s known field keys (see
        BACKEND_FIELD_SPECS): every key must be one of that backend's
        declared settings -- unknown keys are rejected rather than silently
        written, so a typo or a field meant for a different backend never
        lands in config.sh -- and every value must be a plain string."""
        known_keys = {spec.key for spec in BACKEND_FIELD_SPECS.get(backend, [])}
        updates: Dict[str, str] = {}
        for key, value in config.items():
            if key not in known_keys:
                raise ValueError(
                    f"unknown config key for backend '{backend}': {key!r} "
                    f"(known keys: {sorted(known_keys)})"
                )
            if not isinstance(value, str):
                raise ValueError(
                    f"{key}: value must be a string, got {type(value).__name__}"
                )
            updates[key] = value
        return updates

    def set_backend_config(self, backend: str, config: Dict[str, Any]) -> List[dict]:
        """Persist `config` (validated against backend's known keys) into
        config.sh, then return the freshly-read effective settings (same
        shape as get_backend_settings) so callers see exactly what took
        effect."""
        self._require_engine()
        updates = self._validate_backend_config(backend, config)
        if updates:
            self._write_config_values(updates)
        return self.get_backend_settings(backend)

    # ------------------------------------------------------------------
    # Testing / switching
    # ------------------------------------------------------------------

    def test_connection(self, backend: str) -> BackendTestResult:
        """
        Test backend connection by running the real backend_status() function
        from backends/{backend}.sh, in an environment that faithfully mirrors
        what kirocrew-sync.sh itself sets up before calling it -- see
        _LOG_HELPERS_SH and the two defects this fixes:

          1. kirocrew-sync.sh sources config.sh, *then* the backend script
             (in that order -- see its top-level code), so a value the user
             just saved via PUT /backends/{name}/config is what backend_status
             actually sees. A bare `source backends/x.sh` skips config.sh
             entirely and backend_status falls back to the backend script's
             own placeholder default (e.g. S3_BUCKET="your-bucket-name"),
             silently testing the wrong configuration.
          2. log_info/log_success/log_warn/log_error are defined by
             kirocrew-sync.sh itself, not by backends/*.sh (see
             _LOG_HELPERS_SH's docstring). Without them, every log_* call
             inside backend_status fails with "command not found" and the
             test's pass/fail outcome ends up being that unrelated failure
             rather than a real connectivity check.

        The sync directory is passed via subprocess `cwd=`, and config.sh /
        the backend script are referenced by absolute, shell-quoted path --
        never by an unquoted `cd` into shell-string-interpolated text. A
        `cwd` that does not exist (or is not a directory) makes
        subprocess.run raise immediately, which is caught below as a failed
        test -- rather than the old unquoted `cd` silently failing (no
        `set -e`) and leaving `source backends/{backend}.sh` to resolve
        against the server process's own working directory, which could
        report a nonexistent install as a successful connection.
        """
        try:
            backend_script = self.sync_dir / "backends" / f"{backend}.sh"

            # `set -euo pipefail` matches kirocrew-sync.sh's own top-level
            # `set -euo pipefail` (see that file's line 13): without it, a
            # config.sh with a shell syntax error fails its `source` but
            # execution carries on regardless (no `set -e`), silently
            # falling through to the backend script's own defaults and
            # reporting a bogus "connection successful" for a broken config.
            script_lines = ["set -euo pipefail", _LOG_HELPERS_SH]
            if self.config_file.exists():
                script_lines.append(f"source {shlex.quote(str(self.config_file))}")
            script_lines.append(f"source {shlex.quote(str(backend_script))}")
            script_lines.append("backend_status")
            script = "\n".join(script_lines)

            result = subprocess.run(
                ["bash", "-c", script],
                cwd=self.sync_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                return BackendTestResult(
                    success=True,
                    message=f"{backend} connection successful",
                    details={"output": result.stdout}
                )
            else:
                return BackendTestResult(
                    success=False,
                    message=f"{backend} connection failed",
                    details={"error": result.stderr}
                )

        except subprocess.TimeoutExpired:
            return BackendTestResult(
                success=False,
                message="Connection test timed out",
                details={"error": "Timeout after 30 seconds"}
            )
        except Exception as e:
            return BackendTestResult(
                success=False,
                message=f"Test failed: {str(e)}",
                details={"error": str(e)}
            )

    def switch_backend(self, backend: str, config: Optional[Dict[str, Any]] = None) -> bool:
        """
        Switch to a different backend, applying any `config` values passed
        alongside it (validated the same way set_backend_config validates
        them -- unknown keys raise ValueError) as part of the same write, so
        a switch and its settings never land only half-applied.
        """
        self._require_engine()
        updates: Dict[str, str] = {}
        if config:
            updates.update(self._validate_backend_config(backend, config))
        updates["SYNC_BACKEND"] = backend
        self._write_config_values(updates)
        return True
