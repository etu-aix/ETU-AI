"""Local address of the configured MAIA-Beacon instance."""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "ai-server" / "config.json"
DEFAULT_PORT = 11343

def beacon_port():
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        port = config.get("beacon_port", DEFAULT_PORT)
        if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
            return port
    except (OSError, ValueError, AttributeError):
        pass
    return DEFAULT_PORT

def beacon_url():
    return f"http://127.0.0.1:{beacon_port()}"

def companion_pid_file(root):
    """Return a per-installation PID file path in the current user's temp folder."""
    install_root = os.path.normcase(str(Path(root).resolve()))
    identity = hashlib.sha256(install_root.encode("utf-8")).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"etu-ai-companion-{identity}.pid"
