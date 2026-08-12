"""
Pydantic models for KiroCrew Sync app.
"""

from datetime import datetime
from typing import Dict, List, Optional, Literal
from pydantic import BaseModel, Field


# Shared across backend models and server.py path params.
BackendName = Literal["gdrive", "s3", "rsync", "local"]


# Sync run models
class SyncResult(BaseModel):
    """Result of a sync operation."""
    exit_code: int
    duration_ms: int
    scope: Literal["personal", "team"]
    strategy: str
    dry_run: bool = False
    changes_detected: bool = False
    rows_merged: int = 0
    conflicts_count: int = 0
    quarantine_count: int = 0
    output: str
    error: Optional[str] = None


class SyncTriggerRequest(BaseModel):
    """Optional body for POST /sync. Every field defaults, so a body-less
    POST (as sent by StatusWidget.tsx) is equivalent to {}.

    `strategy` is constrained to the values kirocrew-sync.sh actually
    accepts (see its `--strategy` case statement): anything else is a
    FastAPI 422 at the request-validation boundary, never reaching
    SyncManager.run_sync. Previously a bare `str` let a client typo (e.g.
    "bogus") through to the bash script, which exits 1 *before*
    cmd_sync() ever truncates conflicts.jsonl/quarantine.txt -- so the
    artifact-ingestion step would re-record a previous run's leftover
    conflicts/quarantine as new, unboundedly, on every retry."""
    strategy: Literal["auto", "local-wins", "remote-wins", "manual"] = "auto"
    team: bool = False
    dry_run: bool = False


class SyncChange(BaseModel):
    """Individual change detected in sync."""
    change_type: Literal["knowledge", "artifact", "lesson", "transcript", "config"]
    action: Literal["added", "updated", "deleted", "conflict"]
    item_id: Optional[str] = None
    details: Optional[str] = None


class SyncRun(BaseModel):
    """Sync run record."""
    id: int
    timestamp: datetime
    exit_code: int
    duration_ms: Optional[int]
    scope: Literal["personal", "team"]
    strategy: Optional[str]
    dry_run: bool
    changes_detected: bool
    rows_merged: int
    conflicts_count: int
    quarantine_count: int
    output: Optional[str]
    error: Optional[str]


class SyncRunDetails(SyncRun):
    """Sync run with full change details."""
    changes: List[SyncChange] = []


# Conflict models
class Conflict(BaseModel):
    """Unresolved conflict."""
    id: int
    run_id: int
    machine: str
    table_name: str
    row_id: str
    local_value: Optional[str]
    remote_value: Optional[str]
    resolved: bool = False
    resolution: Optional[Literal["local-wins", "remote-wins", "manual"]] = None
    resolved_at: Optional[datetime] = None


class ConflictResolution(BaseModel):
    """Conflict resolution request."""
    resolution: Literal["local-wins", "remote-wins"]


# Quarantine models
class QuarantinedMachine(BaseModel):
    """Machine in quarantine."""
    id: int
    machine: str
    reason: Literal["version_mismatch", "embedding_mismatch", "scope_mismatch", "other"]
    detected_at: datetime
    cleared_at: Optional[datetime] = None
    details: Optional[str] = None


# Status models
class SyncStatus(BaseModel):
    """Current sync status."""
    state: Literal["idle", "syncing", "conflict", "failed", "quarantine"]
    last_sync: Optional[datetime] = None
    next_sync: Optional[datetime] = None
    scope: Literal["personal", "team"]
    machines_active: int
    machines_quarantined: int
    conflicts_pending: int


# Daemon models
class DaemonConfig(BaseModel):
    """Daemon configuration."""
    enabled: bool
    scope: Literal["personal", "team"]
    interval: int = Field(ge=60, le=900, description="Interval in seconds")


class DaemonControl(BaseModel):
    """Daemon control request."""
    action: Literal["start", "stop", "restart"]


# Backend models
class BackendInfo(BaseModel):
    """Backend information."""
    name: BackendName
    display_name: str
    description: str
    configured: bool
    active: bool
    requires_config: List[str] = []


class BackendConfig(BaseModel):
    """Body for POST /backends/switch. `config` (if given) is applied to the
    target backend's config.sh settings as part of the same switch -- see
    BackendManager.switch_backend."""
    backend: BackendName
    config: dict = {}


class BackendTestRequest(BaseModel):
    """Body for POST /backends/test."""
    backend: BackendName


class BackendTestResult(BaseModel):
    """Backend connection test result."""
    success: bool
    message: str
    details: Optional[dict] = None


class BackendConfigField(BaseModel):
    """One backend setting's effective value plus provenance.

    `value` is redacted (see BackendManager._redact) when the key looks
    credential-shaped; `source` says whether the value came from config.sh,
    the process environment, or is just the backend script's own default;
    `is_set` is `source != "default"`; `required` marks settings that have
    no usable default (e.g. S3_BUCKET) -- see BackendManager's
    BACKEND_FIELD_SPECS and its `_check_backend_configured` docstring for
    the exact rule.
    """
    key: str
    value: str
    source: Literal["config", "env", "default"]
    is_set: bool
    required: bool


class BackendConfigStatus(BaseModel):
    """Response for GET and PUT /backends/{name}/config."""
    backend: BackendName
    fields: List[BackendConfigField]


class BackendConfigUpdate(BaseModel):
    """Body for PUT /backends/{name}/config. Keys are validated against
    that backend's known settings server-side; unknown keys are rejected
    (400) rather than silently written to config.sh."""
    config: Dict[str, str] = {}


# API response models
class SyncStatusResponse(BaseModel):
    """Status endpoint response."""
    status: SyncStatus


class HistoryResponse(BaseModel):
    """History endpoint response."""
    runs: List[SyncRun]
    total: int
    limit: int
    offset: int


class ConflictsResponse(BaseModel):
    """Conflicts endpoint response."""
    conflicts: List[Conflict]
    total: int


class QuarantineResponse(BaseModel):
    """Quarantine endpoint response."""
    machines: List[QuarantinedMachine]
    total: int


class BackendsResponse(BaseModel):
    """Backends endpoint response."""
    current: str
    available: List[BackendInfo]


class SyncTriggerResponse(BaseModel):
    """Sync trigger response.

    `success`/`exit_code` are additive: existing clients that only read
    `started`/`message` keep working unchanged, but a client no longer has
    to parse English out of `message` to know whether the sync actually
    succeeded. `success` mirrors the exit-code contract used elsewhere
    (0 = ok, 3 = ok-with-quarantine, anything else = failed); it is False
    (not None) when `started` is False, since no sync ran to succeed."""
    started: bool
    success: bool = False
    exit_code: Optional[int] = None
    run_id: Optional[int] = None
    message: str
