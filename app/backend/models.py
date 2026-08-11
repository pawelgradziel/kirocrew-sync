"""
Pydantic models for KiroCrew Sync app.
"""

from datetime import datetime
from typing import List, Optional, Literal
from pydantic import BaseModel, Field


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
    name: Literal["gdrive", "s3", "rsync", "local"]
    display_name: str
    description: str
    configured: bool
    active: bool
    requires_config: List[str] = []


class BackendConfig(BaseModel):
    """Backend configuration."""
    backend: Literal["gdrive", "s3", "rsync", "local"]
    config: dict


class BackendTestResult(BaseModel):
    """Backend connection test result."""
    success: bool
    message: str
    details: Optional[dict] = None


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
    """Sync trigger response."""
    started: bool
    run_id: Optional[int] = None
    message: str
