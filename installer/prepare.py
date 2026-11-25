"""Create the application environment during a Windows installation."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent.parent
venv_python = root / ".venv" / "Scripts" / "python.exe"
subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")], check=True)
subprocess.run([str(venv_python), "-m", "pip", "install", "-r", str(root / "requirements-app.txt"),
                "-r", str(root / "requirements-desktop.txt")], check=True)
