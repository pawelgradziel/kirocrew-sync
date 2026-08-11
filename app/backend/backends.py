"""
Backend manager - handles backend configuration and testing.
"""

import subprocess
from pathlib import Path
from typing import List, Dict, Any

from .models import BackendInfo, BackendTestResult


class BackendManager:
    """Manages storage backend configuration."""
    
    def __init__(self):
        self.sync_dir = Path.home() / ".kiro/crew/workspace/kirocrew-sync"
        self.config_file = self.sync_dir / "config.sh"
    
    def get_current_backend(self) -> str:
        """Get currently configured backend from config.sh."""
        if not self.config_file.exists():
            return "gdrive"  # default
        
        try:
            with open(self.config_file) as f:
                for line in f:
                    if line.startswith("export SYNC_BACKEND="):
                        backend = line.split("=", 1)[1].strip().strip('"\'')
                        return backend
        except Exception:
            pass
        
        return "gdrive"
    
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
        """Check if backend is configured."""
        backend_file = self.sync_dir / "backends" / f"{backend}.sh"
        return backend_file.exists()
    
    def test_connection(self, backend: str) -> BackendTestResult:
        """
        Test backend connection.
        
        Runs backend's status check to verify connectivity.
        """
        try:
            # Source backend script and run status function
            script = f"""
            cd {self.sync_dir}
            source backends/{backend}.sh
            backend_status
            """
            
            result = subprocess.run(
                ["bash", "-c", script],
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
    
    def switch_backend(self, backend: str, config: Dict[str, Any]) -> bool:
        """
        Switch to different backend.
        
        Updates config.sh with new backend selection.
        """
        if not self.config_file.exists():
            return False
        
        try:
            # Read existing config
            with open(self.config_file) as f:
                lines = f.readlines()
            
            # Update SYNC_BACKEND line
            updated = False
            for i, line in enumerate(lines):
                if line.startswith("export SYNC_BACKEND="):
                    lines[i] = f'export SYNC_BACKEND="{backend}"\n'
                    updated = True
                    break
            
            # Add if not found
            if not updated:
                lines.append(f'export SYNC_BACKEND="{backend}"\n')
            
            # Write back
            with open(self.config_file, 'w') as f:
                f.writelines(lines)
            
            return True
        
        except Exception:
            return False
