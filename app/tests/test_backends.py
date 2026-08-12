"""
Tests for backend/backends.py: BackendManager's configured-detection,
effective-settings reads, config.sh persistence (including the hostile-value
shell-injection defenses), unknown-key rejection, and idempotent rewriting.

SAFETY: every test constructs BackendManager with an explicit `sync_dir`
under `tmp_path` (see conftest.py's `_fake_home` autouse fixture for the
belt-and-suspenders backstop) -- none of these tests ever touch the real
`~/.kiro/crew` or the repo's own config.sh. The hostile-value sourcing tests
additionally run bash with `$HOME` pointed at a disposable directory (see
`injection_home`), so that even a literal `rm -rf ~` in a hostile value
would -- if the injection defenses ever regressed -- only ever delete that
throwaway directory, never a real home directory.
"""

import os
import subprocess

import pytest

from backend import backends
from backend.backends import BackendManager
from backend.sync_manager import SyncEngineUnavailable


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture
def sync_dir(tmp_path):
    """A throwaway sync_dir with a stub kirocrew-sync.sh, so BackendManager
    considers the engine "installed" and write operations don't raise
    SyncEngineUnavailable."""
    d = tmp_path / "sync"
    d.mkdir()
    (d / "kirocrew-sync.sh").write_text("#!/usr/bin/env bash\n")
    return d


@pytest.fixture
def mgr(sync_dir):
    return BackendManager(sync_dir=sync_dir)


@pytest.fixture
def injection_home(tmp_path):
    """A throwaway $HOME for the sourcing subprocess in the hostile-value
    tests. See module docstring."""
    d = tmp_path / "injection_home"
    d.mkdir()
    (d / "canary").write_text("still here")
    return d


def _write_config(sync_dir, text):
    (sync_dir / "config.sh").write_text(text)


def _source_value(sync_dir, key, home):
    """Actually `source` the config.sh this test just wrote, in a real bash
    subprocess, and print the resulting value of `key`. This is the proof
    that a hostile value round-trips as inert *data* rather than being
    executed: if injection succeeded, stdout would contain output from the
    injected command(s) instead of (or in addition to) the literal value."""
    return subprocess.run(
        ["bash", "-c", f'source "{sync_dir}/config.sh"; printf %s "${{{key}:-}}"'],
        capture_output=True,
        text=True,
        timeout=5,
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )


# ---------------------------------------------------------------------
# Quoting/validation unit tests
# ---------------------------------------------------------------------

def test_quote_shell_value_escapes_embedded_single_quote():
    assert backends._quote_shell_value("it's") == "'it'\\''s'"


def test_validate_config_value_rejects_newline():
    with pytest.raises(ValueError):
        backends._validate_config_value("KEY", "a\nb")


def test_validate_config_value_rejects_control_characters():
    with pytest.raises(ValueError):
        backends._validate_config_value("KEY", "a\x00b")


def test_validate_config_value_allows_shell_metacharacters():
    # Metacharacters like `$()` and `;` are exactly what quoting (not
    # validation) must neutralize -- see the round-trip sourcing tests
    # below, which prove quoting actually does that.
    backends._validate_config_value("KEY", "$(rm -rf /)")
    backends._validate_config_value("KEY", "; echo hi ;")


# ---------------------------------------------------------------------
# configured-detection
# ---------------------------------------------------------------------

def test_local_is_configured_with_no_config_file(mgr):
    """local's only field (LOCAL_SYNC_DIR) has a usable default and no
    external command dependency, so it is configured out of the box."""
    assert mgr._check_backend_configured("local") is True


def test_gdrive_not_configured_without_rclone_installed(mgr, monkeypatch):
    monkeypatch.setattr(backends.shutil, "which", lambda cmd: None)
    assert mgr._check_backend_configured("gdrive") is False


def test_gdrive_configured_with_rclone_present_and_only_defaults(mgr, monkeypatch):
    monkeypatch.setattr(
        backends.shutil, "which", lambda cmd: f"/usr/bin/{cmd}" if cmd == "rclone" else None
    )
    assert mgr._check_backend_configured("gdrive") is True


def test_s3_not_configured_without_bucket(mgr, monkeypatch):
    monkeypatch.setattr(backends.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    assert mgr._check_backend_configured("s3") is False


def test_s3_configured_once_bucket_is_set(mgr, monkeypatch):
    monkeypatch.setattr(backends.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    mgr.set_backend_config("s3", {"S3_BUCKET": "my-bucket"})
    assert mgr._check_backend_configured("s3") is True


def test_s3_not_configured_without_aws_cli_even_with_bucket_set(mgr, monkeypatch):
    monkeypatch.setattr(backends.shutil, "which", lambda cmd: None)
    mgr.set_backend_config("s3", {"S3_BUCKET": "my-bucket"})
    assert mgr._check_backend_configured("s3") is False


def test_s3_bucket_from_environment_counts_as_configured(mgr, monkeypatch):
    monkeypatch.setattr(backends.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setenv("S3_BUCKET", "env-bucket")
    assert mgr._check_backend_configured("s3") is True


def test_rsync_requires_both_host_and_path(mgr, monkeypatch):
    monkeypatch.setattr(backends.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    assert mgr._check_backend_configured("rsync") is False

    mgr.set_backend_config("rsync", {"RSYNC_HOST": "user@host"})
    assert mgr._check_backend_configured("rsync") is False

    mgr.set_backend_config("rsync", {"RSYNC_PATH": "/remote/path"})
    assert mgr._check_backend_configured("rsync") is True


def test_rsync_not_configured_without_rsync_installed(mgr, monkeypatch):
    monkeypatch.setattr(backends.shutil, "which", lambda cmd: None)
    mgr.set_backend_config("rsync", {"RSYNC_HOST": "user@host", "RSYNC_PATH": "/remote/path"})
    assert mgr._check_backend_configured("rsync") is False


# ---------------------------------------------------------------------
# get_backend_settings (effective values + provenance)
# ---------------------------------------------------------------------

def test_get_backend_settings_shows_defaults_when_unset(mgr):
    fields = {f["key"]: f for f in mgr.get_backend_settings("gdrive")}
    assert fields["GDRIVE_REMOTE_NAME"]["value"] == "kirocrew-gdrive"
    assert fields["GDRIVE_REMOTE_NAME"]["source"] == "default"
    assert fields["GDRIVE_REMOTE_NAME"]["is_set"] is False
    assert fields["GDRIVE_REMOTE_NAME"]["required"] is False


def test_get_backend_settings_flags_required_field(mgr):
    fields = {f["key"]: f for f in mgr.get_backend_settings("s3")}
    assert fields["S3_BUCKET"]["required"] is True
    assert fields["S3_BUCKET"]["is_set"] is False
    assert fields["S3_PREFIX"]["required"] is False


def test_get_backend_settings_reflects_config_file(mgr):
    mgr.set_backend_config("s3", {"S3_BUCKET": "my-bucket"})
    fields = {f["key"]: f for f in mgr.get_backend_settings("s3")}
    assert fields["S3_BUCKET"]["value"] == "my-bucket"
    assert fields["S3_BUCKET"]["source"] == "config"
    assert fields["S3_BUCKET"]["is_set"] is True


def test_config_file_value_takes_precedence_over_environment(mgr, monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "env-bucket")
    mgr.set_backend_config("s3", {"S3_BUCKET": "config-bucket"})
    fields = {f["key"]: f for f in mgr.get_backend_settings("s3")}
    assert fields["S3_BUCKET"]["value"] == "config-bucket"
    assert fields["S3_BUCKET"]["source"] == "config"


def test_credential_shaped_key_is_redacted():
    """None of the current backend fields are credential-shaped (see
    backends.py module docstring), so exercise the redaction helper
    directly to prove the backstop actually redacts when a key does
    match."""
    assert backends._redact("AWS_SECRET_ACCESS_KEY", "topsecret") == "***REDACTED***"
    assert backends._redact("S3_BUCKET", "my-bucket") == "my-bucket"


# ---------------------------------------------------------------------
# Persistence: round trip, in-place update, idempotence, preservation
# ---------------------------------------------------------------------

def test_set_backend_config_round_trips(mgr):
    mgr.set_backend_config("rsync", {"RSYNC_HOST": "user@host", "RSYNC_PATH": "/data"})
    fields = {f["key"]: f["value"] for f in mgr.get_backend_settings("rsync")}
    assert fields["RSYNC_HOST"] == "user@host"
    assert fields["RSYNC_PATH"] == "/data"


def test_set_backend_config_preserves_unrelated_lines(mgr, sync_dir):
    _write_config(
        sync_dir,
        "#!/usr/bin/env bash\n"
        "# a comment that must survive\n"
        'export SYNC_BACKEND="gdrive"\n'
        "export OTHER_THING=1\n",
    )
    mgr.set_backend_config("s3", {"S3_BUCKET": "my-bucket"})
    text = (sync_dir / "config.sh").read_text()
    assert "# a comment that must survive" in text
    assert 'export SYNC_BACKEND="gdrive"' in text
    assert "export OTHER_THING=1" in text
    assert "export S3_BUCKET='my-bucket'" in text


def test_set_backend_config_updates_existing_line_in_place(mgr, sync_dir):
    mgr.set_backend_config("s3", {"S3_BUCKET": "first-bucket"})
    before_lines = (sync_dir / "config.sh").read_text().splitlines()

    mgr.set_backend_config("s3", {"S3_BUCKET": "second-bucket"})
    after_text = (sync_dir / "config.sh").read_text()
    after_lines = after_text.splitlines()

    assert len(after_lines) == len(before_lines)
    assert sum(1 for line in after_lines if "S3_BUCKET" in line) == 1
    assert "second-bucket" in after_text
    assert "first-bucket" not in after_text


def test_set_backend_config_is_idempotent(mgr):
    sync_dir = mgr.sync_dir
    mgr.set_backend_config("s3", {"S3_BUCKET": "my-bucket", "AWS_PROFILE": "work"})
    first = (sync_dir / "config.sh").read_text()

    mgr.set_backend_config("s3", {"S3_BUCKET": "my-bucket", "AWS_PROFILE": "work"})
    second = (sync_dir / "config.sh").read_text()

    assert first == second


# ---------------------------------------------------------------------
# Unknown-key rejection
# ---------------------------------------------------------------------

def test_set_backend_config_rejects_unknown_key(mgr):
    with pytest.raises(ValueError):
        mgr.set_backend_config("s3", {"NOT_A_REAL_KEY": "x"})


def test_set_backend_config_rejects_key_belonging_to_a_different_backend(mgr):
    with pytest.raises(ValueError):
        mgr.set_backend_config("s3", {"RSYNC_HOST": "user@host"})


def test_rejected_config_writes_nothing_at_all(mgr, sync_dir):
    """An unknown key anywhere in the payload must reject the whole write --
    not silently persist the valid keys alongside it."""
    with pytest.raises(ValueError):
        mgr.set_backend_config("s3", {"S3_BUCKET": "should-not-be-written", "EVIL": "x"})

    config_file = sync_dir / "config.sh"
    assert not config_file.exists() or "should-not-be-written" not in config_file.read_text()


# ---------------------------------------------------------------------
# switch_backend
# ---------------------------------------------------------------------

def test_switch_backend_updates_sync_backend_and_applies_config(mgr):
    mgr.switch_backend("s3", {"S3_BUCKET": "my-bucket"})
    assert mgr.get_current_backend() == "s3"
    fields = {f["key"]: f["value"] for f in mgr.get_backend_settings("s3")}
    assert fields["S3_BUCKET"] == "my-bucket"


def test_switch_backend_without_config_still_switches(mgr):
    mgr.switch_backend("local", {})
    assert mgr.get_current_backend() == "local"


def test_switch_backend_raises_when_engine_not_installed(tmp_path):
    empty_dir = tmp_path / "no-engine"
    empty_dir.mkdir()
    m = BackendManager(sync_dir=empty_dir)
    with pytest.raises(SyncEngineUnavailable):
        m.switch_backend("local", {})


# ---------------------------------------------------------------------
# Hostile values: injection defenses, proven by actually sourcing the file
# ---------------------------------------------------------------------

def test_semicolon_and_command_injection_is_inert_when_sourced(mgr, sync_dir, injection_home):
    hostile = "; rm -rf ~; echo pwned"
    mgr.set_backend_config("s3", {"S3_BUCKET": hostile})

    # Round-trips as the literal string through this app's own parser too.
    fields = {f["key"]: f["value"] for f in mgr.get_backend_settings("s3")}
    assert fields["S3_BUCKET"] == hostile

    result = _source_value(sync_dir, "S3_BUCKET", injection_home)
    assert result.returncode == 0
    assert result.stdout == hostile
    # `~` (injection_home) was never touched, even though the hostile value
    # names it with `rm -rf ~`.
    assert (injection_home / "canary").exists()


def test_command_substitution_is_inert_when_sourced(mgr, sync_dir, injection_home, tmp_path):
    marker = tmp_path / "SUBSHELL_RAN"
    hostile = f"$(touch {marker})"
    mgr.set_backend_config("s3", {"S3_BUCKET": hostile})

    result = _source_value(sync_dir, "S3_BUCKET", injection_home)
    assert result.returncode == 0
    assert result.stdout == hostile
    assert not marker.exists()


def test_embedded_single_quote_round_trips_literally(mgr, sync_dir, injection_home):
    hostile = "it's a test' ; echo pwned #"
    mgr.set_backend_config("local", {"LOCAL_SYNC_DIR": hostile})

    fields = {f["key"]: f["value"] for f in mgr.get_backend_settings("local")}
    assert fields["LOCAL_SYNC_DIR"] == hostile

    result = _source_value(sync_dir, "LOCAL_SYNC_DIR", injection_home)
    assert result.returncode == 0
    assert result.stdout == hostile


def test_newline_injection_is_rejected_and_never_written(mgr, sync_dir):
    """A raw newline in a value could inject a second `export` line -- this
    is rejected outright rather than trusted to quoting (see
    _validate_config_value's docstring)."""
    hostile = "ok\nexport EVIL=1"

    with pytest.raises(ValueError):
        mgr.set_backend_config("s3", {"S3_BUCKET": hostile})

    config_file = sync_dir / "config.sh"
    assert not config_file.exists() or "EVIL" not in config_file.read_text()
