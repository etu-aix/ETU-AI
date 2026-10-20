"""Optional desktop handoff; the Wiki itself has no Obsidian dependency."""
import os
import shutil
import subprocess
import sys
from urllib.parse import quote


def obsidian_available():
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "obsidian"):
                return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return subprocess.run(["open", "-Ra", "Obsidian"], capture_output=True, check=False).returncode == 0
    return bool(shutil.which("obsidian"))


def location(root):
    root = root.resolve()
    registered = (root / ".obsidian").is_dir()
    first_note = next((path for path in root.rglob("*.md") if path.is_file()), None) if registered else None
    uri = ("obsidian://open?path=" + quote(str(first_note), safe="") if first_note else
           "obsidian://open?vault=" + quote(root.name, safe="")) if registered else None
    return {"path": str(root), "obsidian_available": obsidian_available(),
            "obsidian_ready": registered,
            "obsidian_uri": uri}


def launch(target):
    if sys.platform == "win32":
        os.startfile(target)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", target], start_new_session=True)
    else:
        subprocess.Popen(["xdg-open", target], start_new_session=True)
