"""Local address of the configured MAIA-Beacon instance."""
import json
import sys
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
