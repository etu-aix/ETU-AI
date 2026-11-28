"""Detached handoff used when the application changes launch mode."""

import socket
import sys
import time

from run_launcher import LAUNCH_MODES, save_launch_mode, start_server


def ports_available() -> bool:
    listeners = []
    try:
        for port in (8900, 8901):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listeners.append(listener)
            listener.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        for listener in listeners:
            listener.close()


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode not in LAUNCH_MODES:
        return 2
    save_launch_mode(mode)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and not ports_available():
        time.sleep(0.25)
    if not ports_available():
        return 1
    return start_server(mode).wait()


if __name__ == "__main__":
    raise SystemExit(main())
