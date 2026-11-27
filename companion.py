"""Desktop companion: a persistent webview and a configurable global shortcut."""
import os
import socket
import json
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request

from storage.db import StorageManager
from beacon_address import companion_pid_file

SHORTCUTS = {
    "ctrl-alt-space": "<ctrl>+<alt>+<space>",
    "ctrl-shift-space": "<ctrl>+<shift>+<space>",
    "ctrl-alt-e": "<ctrl>+<alt>+e",
}
DEFAULT_SHORTCUT = "ctrl-alt-space"


class Companion:
    def __init__(self, window, storage):
        self.window = window
        self.storage = storage
        self.visible = False
        self.stopping = False
        self.lock = threading.RLock()

    def toggle(self):
        with self.lock:
            if self.visible:
                self.window.hide()
                self.visible = False
            else:
                self.window.show()
                self.visible = True

    def close(self):
        with self.lock:
            if self.stopping:
                return True
            self.window.hide()
            self.visible = False
        return False

    def moved(self, x, y):
        self.storage.set_setting("companion_position", {"x": int(x), "y": int(y)})


def listen_for_shortcut(companion, keyboard):
    listener = None
    current = None
    try:
        while True:
            shortcut = companion.storage.get_setting("companion_shortcut", DEFAULT_SHORTCUT)
            if shortcut not in SHORTCUTS:
                shortcut = DEFAULT_SHORTCUT
            if shortcut != current:
                replacement = keyboard.GlobalHotKeys({SHORTCUTS[shortcut]: companion.toggle})
                replacement.start()
                if listener:
                    listener.stop()
                listener = replacement
                current = shortcut
            time.sleep(1)
    finally:
        if listener:
            listener.stop()


def watch_server(companion):
    """Avoid leaving an invisible process after the server has stopped."""
    unavailable_since = None
    server_pid = None
    while True:
        time.sleep(5)
        try:
            with urllib.request.urlopen("http://127.0.0.1:8900/v1/status", timeout=2) as response:
                if response.status != 200:
                    raise OSError("server unavailable")
            unavailable_since = None
            try:
                with urllib.request.urlopen("http://127.0.0.1:8900/api/launch-mode", timeout=2) as response:
                    running_pid = json.load(response).get("pid")
                if isinstance(running_pid, int):
                    if server_pid is not None and running_pid != server_pid:
                        companion.window.load_url("http://127.0.0.1:8900/?companion=1")
                    server_pid = running_pid
            except (OSError, urllib.error.URLError, ValueError):
                # Launch mode is unavailable during sleep; the status probe still keeps the window alive.
                pass
        except (OSError, urllib.error.URLError):
            if unavailable_since is None:
                unavailable_since = time.monotonic()
            elif time.monotonic() - unavailable_since >= 60:
                companion.stopping = True
                companion.window.destroy()
                return


def main():
    show_on_start = "--show" in sys.argv[1:]
    requested_pid_file = os.environ.get("ETU_COMPANION_PID_FILE")
    expected_pid_file = companion_pid_file(Path(__file__).resolve().parent)
    pid_file = expected_pid_file if requested_pid_file and Path(requested_pid_file).resolve() == expected_pid_file.resolve() else None
    # A second invocation must not register the same shortcut twice.
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 8903))
    except OSError:
        return 0
    try:
        if pid_file:
            try:
                pid_file.write_text(str(os.getpid()), encoding="ascii")
            except OSError:
                pid_file = None
        import webview
        from pynput import keyboard

        storage = StorageManager()
        position = storage.get_setting("companion_position", {})
        options = {}
        if isinstance(position, dict) and type(position.get("x")) is int and type(position.get("y")) is int:
            options = {"x": position["x"], "y": position["y"]}
        window = webview.create_window("ETU-AI", "http://127.0.0.1:8900/?companion=1", width=440, height=680,
                                       min_size=(320, 420), hidden=not show_on_start, on_top=True, **options)
        companion = Companion(window, storage)
        companion.visible = show_on_start
        window.events.closing += companion.close
        window.events.moved += companion.moved
        threading.Thread(target=listen_for_shortcut, args=(companion, keyboard), daemon=True).start()
        threading.Thread(target=watch_server, args=(companion,), daemon=True).start()
        icon = Path(__file__).resolve().parent / "assets" / "etu-ai_logo.ico"
        webview.start(private_mode=False, icon=str(icon) if icon.is_file() else None)
    finally:
        lock.close()
        if pid_file:
            try:
                if pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
                    pid_file.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
