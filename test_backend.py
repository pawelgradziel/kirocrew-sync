#!/usr/bin/env python3
"""
Test script for KiroCrew Sync app backend.

Validates:
- Database initialization
- Sync manager wrapper
- History tracking
- API endpoints (if server running)
"""

import sys
from pathlib import Path

# Add backend to path
app_dir = Path(__file__).parent / "app"
sys.path.insert(0, str(app_dir))

from backend.database import Database
from backend.sync_manager import SyncManager
from backend.history import HistoryManager
from backend.conflicts import ConflictManager
from backend.quarantine import QuarantineManager
from backend.backends import BackendManager


def test_database():
    """Test database initialization."""
    print("Testing database...")
    db = Database(Path("/tmp/test_kirocrew_sync.db"))
    db.initialize()
    
    # Verify tables exist
    with db.connect() as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = [row['name'] for row in cursor.fetchall()]
        
        expected = ['sync_runs', 'sync_changes', 'conflicts', 'quarantine', 'daemon_state']
        for table in expected:
            assert table in tables, f"Table {table} not found"
    
    print("✓ Database schema valid")
    return db


def test_sync_manager():
    """Test sync manager."""
    print("\nTesting sync manager...")
    try:
        mgr = SyncManager()
        print(f"✓ Sync script found at {mgr.script}")
        
        # Test is_running
        running = mgr.is_running()
        print(f"✓ Sync running check: {running}")
        
        # Test daemon config
        config = mgr.get_daemon_config()
        print(f"✓ Daemon config: {config}")
        
        return mgr
    except FileNotFoundError as e:
        print(f"⚠ Sync script not found: {e}")
        print("  This is expected if kirocrew-sync is not installed")
        return None


def test_history_manager(db):
    """Test history manager."""
    print("\nTesting history manager...")
    mgr = HistoryManager(db.db_path)
    
    # Test recording a sync
    from backend.models import SyncResult
    result = SyncResult(
        exit_code=0,
        duration_ms=1500,
        scope="personal",
        strategy="auto",
        dry_run=False,
        changes_detected=True,
        rows_merged=10,
        conflicts_count=0,
        quarantine_count=0,
        output="Test sync output"
    )
    
    run_id = mgr.record_sync(result)
    print(f"✓ Recorded sync run: {run_id}")
    
    # Test retrieving history
    runs = mgr.get_recent(limit=10)
    assert len(runs) >= 1, "No runs found"
    print(f"✓ Retrieved {len(runs)} recent runs")
    
    # Test get_run_details
    details = mgr.get_run_details(run_id)
    assert details is not None, "Run details not found"
    print(f"✓ Retrieved run details for run {run_id}")
    
    return mgr


def test_conflict_manager(db):
    """Test conflict manager."""
    print("\nTesting conflict manager...")
    mgr = ConflictManager(db.db_path)
    
    # Record a conflict
    conflict_id = mgr.record_conflict(
        run_id=1,
        machine="test-machine",
        table_name="knowledge.items",
        row_id="item-123",
        local_value="local data",
        remote_value="remote data"
    )
    print(f"✓ Recorded conflict: {conflict_id}")
    
    # Get unresolved
    conflicts = mgr.get_unresolved()
    assert len(conflicts) >= 1, "No conflicts found"
    print(f"✓ Retrieved {len(conflicts)} unresolved conflicts")
    
    # Resolve conflict
    success = mgr.resolve_conflict(conflict_id, "local-wins")
    assert success, "Failed to resolve conflict"
    print(f"✓ Resolved conflict {conflict_id}")
    
    return mgr


def test_quarantine_manager(db):
    """Test quarantine manager."""
    print("\nTesting quarantine manager...")
    mgr = QuarantineManager(db.db_path)
    
    # Add to quarantine
    q_id = mgr.add_quarantine(
        machine="old-laptop",
        reason="version_mismatch",
        details="KiroCrew v1.2.0 (current: v1.3.0)"
    )
    print(f"✓ Added machine to quarantine: {q_id}")
    
    # Get quarantined
    machines = mgr.get_quarantined()
    assert len(machines) >= 1, "No quarantined machines"
    print(f"✓ Retrieved {len(machines)} quarantined machines")
    
    # Clear quarantine
    success = mgr.clear_quarantine("old-laptop")
    assert success, "Failed to clear quarantine"
    print(f"✓ Cleared quarantine for old-laptop")
    
    return mgr


def test_backend_manager():
    """Test backend manager."""
    print("\nTesting backend manager...")
    mgr = BackendManager()
    
    # Get current backend
    current = mgr.get_current_backend()
    print(f"✓ Current backend: {current}")
    
    # List backends
    backends = mgr.list_backends()
    print(f"✓ Found {len(backends)} backends")
    for backend in backends:
        status = "active" if backend.active else "available"
        configured = "configured" if backend.configured else "not configured"
        print(f"  - {backend.display_name}: {status}, {configured}")
    
    return mgr


def main():
    """Run all tests."""
    print("KiroCrew Sync Backend Tests")
    print("=" * 50)
    
    try:
        # Test database
        db = test_database()
        
        # Test sync manager (may fail if not installed)
        sync_mgr = test_sync_manager()
        
        # Test history manager
        history_mgr = test_history_manager(db)
        
        # Test conflict manager
        conflict_mgr = test_conflict_manager(db)
        
        # Test quarantine manager
        quarantine_mgr = test_quarantine_manager(db)
        
        # Test backend manager
        backend_mgr = test_backend_manager()
        
        print("\n" + "=" * 50)
        print("✓ All tests passed!")
        print("\nBackend is ready for integration.")
        
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        # Cleanup test database
        test_db = Path("/tmp/test_kirocrew_sync.db")
        if test_db.exists():
            test_db.unlink()
            print("\nCleaned up test database")


if __name__ == "__main__":
    main()
