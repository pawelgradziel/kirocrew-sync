"""
FastAPI server for KiroCrew Sync app.
"""

from fastapi import FastAPI, HTTPException
from typing import Optional

from .models import (
    SyncStatusResponse, HistoryResponse, ConflictsResponse,
    QuarantineResponse, BackendsResponse, SyncTriggerResponse,
    ConflictResolution, DaemonConfig, DaemonControl, BackendConfig,
    BackendTestResult, SyncStatus
)
from .sync_manager import SyncManager
from .history import HistoryManager
from .conflicts import ConflictManager
from .quarantine import QuarantineManager
from .backends import BackendManager


app = FastAPI(title="KiroCrew Sync API")

# Initialize managers
sync_mgr = SyncManager()
history_mgr = HistoryManager()
conflict_mgr = ConflictManager()
quarantine_mgr = QuarantineManager()
backend_mgr = BackendManager()


@app.get("/status", response_model=SyncStatusResponse)
async def get_status():
    """Get current sync status."""
    try:
        # Get last sync
        last_sync = history_mgr.get_last_sync()
        
        # Count quarantined machines
        quarantine_count = quarantine_mgr.get_quarantine_count()
        
        # Count conflicts
        conflict_count = conflict_mgr.get_conflict_count()
        
        # Determine state
        if sync_mgr.is_running():
            state = "syncing"
        elif conflict_count > 0:
            state = "conflict"
        elif quarantine_count > 0:
            state = "quarantine"
        elif last_sync and last_sync.exit_code != 0:
            state = "failed"
        else:
            state = "idle"
        
        status = SyncStatus(
            state=state,
            last_sync=last_sync.timestamp if last_sync else None,
            next_sync=None,  # TODO: Calculate from daemon state
            scope=sync_mgr.get_daemon_config()["scope"],
            machines_active=0,  # TODO: Parse from sync repo
            machines_quarantined=quarantine_count,
            conflicts_pending=conflict_count
        )
        
        return SyncStatusResponse(status=status)
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sync", response_model=SyncTriggerResponse)
async def trigger_sync(
    strategy: str = "auto",
    team: bool = False,
    dry_run: bool = False
):
    """Trigger manual sync."""
    try:
        # Check if already running
        if sync_mgr.is_running():
            return SyncTriggerResponse(
                started=False,
                message="Sync already in progress"
            )
        
        # Run sync
        result = sync_mgr.run_sync(strategy=strategy, team=team, dry_run=dry_run)
        
        # Record in history
        run_id = history_mgr.record_sync(result)
        
        return SyncTriggerResponse(
            started=True,
            run_id=run_id,
            message=f"Sync completed with exit code {result.exit_code}"
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/history", response_model=HistoryResponse)
async def get_history(
    limit: int = 50,
    offset: int = 0,
    scope: Optional[str] = None
):
    """Get sync history."""
    try:
        runs = history_mgr.get_recent(limit=limit, offset=offset, scope=scope)
        total = history_mgr.get_total_count(scope=scope)
        
        return HistoryResponse(
            runs=runs,
            total=total,
            limit=limit,
            offset=offset
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/history/{run_id}")
async def get_run_details(run_id: int):
    """Get detailed sync run information."""
    try:
        details = history_mgr.get_run_details(run_id)
        if not details:
            raise HTTPException(status_code=404, detail="Run not found")
        return details
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/conflicts", response_model=ConflictsResponse)
async def get_conflicts():
    """Get unresolved conflicts."""
    try:
        conflicts = conflict_mgr.get_unresolved()
        return ConflictsResponse(
            conflicts=conflicts,
            total=len(conflicts)
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/conflicts/{conflict_id}/resolve")
async def resolve_conflict(conflict_id: int, resolution: ConflictResolution):
    """Resolve a conflict."""
    try:
        success = conflict_mgr.resolve_conflict(conflict_id, resolution.resolution)
        if not success:
            raise HTTPException(status_code=404, detail="Conflict not found")
        
        return {"success": True, "conflict_id": conflict_id, "resolution": resolution.resolution}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/quarantine", response_model=QuarantineResponse)
async def get_quarantine():
    """Get quarantined machines."""
    try:
        machines = quarantine_mgr.get_quarantined()
        return QuarantineResponse(
            machines=machines,
            total=len(machines)
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/quarantine/{machine}/clear")
async def clear_quarantine(machine: str):
    """Clear machine from quarantine."""
    try:
        success = quarantine_mgr.clear_quarantine(machine)
        if not success:
            raise HTTPException(status_code=404, detail="Machine not in quarantine")
        
        return {"success": True, "machine": machine}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/backends", response_model=BackendsResponse)
async def list_backends():
    """List available backends."""
    try:
        current = backend_mgr.get_current_backend()
        available = backend_mgr.list_backends()
        
        return BackendsResponse(
            current=current,
            available=available
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/backends/test", response_model=BackendTestResult)
async def test_backend(backend: str):
    """Test backend connection."""
    try:
        result = backend_mgr.test_connection(backend)
        return result
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/backends/switch")
async def switch_backend(config: BackendConfig):
    """Switch to different backend."""
    try:
        success = backend_mgr.switch_backend(config.backend, config.config)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to switch backend")
        
        return {"success": True, "backend": config.backend}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/daemon/config", response_model=DaemonConfig)
async def get_daemon_config():
    """Get daemon configuration."""
    try:
        config = sync_mgr.get_daemon_config()
        return DaemonConfig(**config)
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/daemon/config", response_model=DaemonConfig)
async def update_daemon_config(config: DaemonConfig):
    """Update daemon configuration."""
    try:
        updated = sync_mgr.update_daemon_config(
            enabled=config.enabled,
            scope=config.scope,
            interval=config.interval
        )
        return DaemonConfig(**updated)
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/daemon/control")
async def control_daemon(control: DaemonControl):
    """Control daemon (start/stop/restart)."""
    try:
        # TODO: Implement daemon control via KiroCrew cron API
        return {"success": True, "action": control.action}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
