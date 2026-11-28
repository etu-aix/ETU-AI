"""
Lanceur 1-Clic pour ETU-AI.
Démarre le serveur local en arrière-plan, vérifie sa disponibilité et ouvre automatiquement
l'interface web dans le navigateur par défaut de l'étudiant.
Compilable en .exe avec PyInstaller.
"""

import os
import json
import argparse
import ipaddress
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

from beacon_address import beacon_port, companion_pid_file

# PyInstaller's windowed bootloader sets these streams to None. Keep the
# existing diagnostic prints harmless when users launch the GUI executable.
_null_stream = open(os.devnull, "w", encoding="utf-8") if getattr(sys, "frozen", False) else None
if _null_stream is not None:
    if sys.stdout is None:
        sys.stdout = _null_stream
    if sys.stderr is None:
        sys.stderr = _null_stream

# A frozen launcher lives beside the installed application files.  In source
# mode, keep the existing script-relative behavior.
ROOT_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
SERVER_URL = "http://127.0.0.1:8900"
BRIDGE_URL = "http://127.0.0.1:8901"
LAUNCH_MODES = ("normal", "local", "server")
LAUNCH_MODE_FILE = ROOT_DIR / "storage" / "launch-mode.json"


def saved_launch_mode() -> str:
    """Return the last selected launch mode, defaulting safely to normal."""
    try:
        mode = json.loads(LAUNCH_MODE_FILE.read_text(encoding="utf-8")).get("mode")
        return mode if mode in LAUNCH_MODES else "normal"
    except (OSError, json.JSONDecodeError, AttributeError):
        return "normal"


def save_launch_mode(mode: str) -> None:
    """Remember the mode chosen from settings for the next launcher start."""
    if mode not in LAUNCH_MODES:
        raise ValueError(f"Mode de lancement inconnu : {mode}")
    LAUNCH_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_MODE_FILE.write_text(json.dumps({"mode": mode}), encoding="utf-8")


def configure_launch_environment(env: dict[str, str], mode: str) -> dict[str, str]:
    """Apply only the network settings associated with the selected mode."""
    if mode not in LAUNCH_MODES:
        raise ValueError(f"Mode de lancement inconnu : {mode}")
    configured = env.copy()
    configured["ETU_LAUNCH_MODE"] = mode
    configured["ETU_ALLOW_LOCALHOST_ORIGINS"] = "1" if mode == "local" else "0"
    return configured


def check_server_ready(url: str, timeout: int = 15) -> bool:
    """Vérifie si le serveur local répond aux requêtes HTTP."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            with urllib.request.urlopen(f"{url}/v1/status", timeout=2) as resp:
                ui_status = json.load(resp)
                if resp.status == 200 and ui_status.get("status") == "ready" and ui_status.get("version") == "1.0.0":
                    with urllib.request.urlopen(f"{BRIDGE_URL}/v1/status", timeout=2) as bridge_resp:
                        bridge_status = json.load(bridge_resp)
                        if bridge_resp.status == 200 and bridge_status == ui_status:
                            return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def start_server(mode: str = "local") -> subprocess.Popen:
    """Démarre le serveur API FastAPI local."""
    env = configure_launch_environment(os.environ, mode)
    env["ETU_LAUNCHER_PID"] = str(os.getpid())
    env["PYTHONPATH"] = str(ROOT_DIR) + os.pathsep + env.get("PYTHONPATH", "")

    # One process serves both the UI and ETU 2 bridge, sharing pairing state.
    if os.name == "nt" and getattr(sys, "frozen", False):
        project_python = ROOT_DIR / "python" / "python.exe"
    else:
        project_python = ROOT_DIR / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if os.name == "nt" and not project_python.is_file():
            project_python = ROOT_DIR / "python" / "python.exe"
    if not project_python.is_file() and sys.prefix == sys.base_prefix:
        raise RuntimeError("Environnement virtuel introuvable. Créez .venv et installez requirements-app.txt avant de lancer ETU-AI.")
    cmd = [
        str(project_python) if project_python.is_file() else sys.executable,
        str(ROOT_DIR / "serve.py"),
    ]

    print("[+] Lancement du serveur ETU-AI sur http://127.0.0.1:8900...")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and getattr(sys, "frozen", False) else 0
    process = subprocess.Popen(
        cmd,
        cwd=str(ROOT_DIR),
        env=env,
        creationflags=flags,
    )
    return process


def start_companion() -> bool:
    """Start the desktop process when its optional dependencies are installed."""
    companion_executable = ROOT_DIR / "ETU-AI-Companion.exe"
    if os.name == "nt" and companion_executable.is_file():
        env = os.environ.copy()
        env["ETU_COMPANION_PID_FILE"] = str(companion_pid_file(ROOT_DIR))
        try:
            flags = subprocess.CREATE_NO_WINDOW if getattr(sys, "frozen", False) else 0
            subprocess.Popen([str(companion_executable)], cwd=str(ROOT_DIR), stdin=subprocess.DEVNULL,
                             creationflags=flags, start_new_session=True, env=env)
            return True
        except OSError:
            return False
    if os.name == "nt" and getattr(sys, "frozen", False):
        return False
    project_python = ROOT_DIR / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if os.name == "nt" and not project_python.is_file():
        project_python = ROOT_DIR / "python" / "python.exe"
    if not project_python.is_file() and sys.prefix == sys.base_prefix:
        return False
    python = str(project_python) if project_python.is_file() else sys.executable
    try:
        check = subprocess.run(
            [python, "-c", "import importlib.util,sys; sys.exit(not all(importlib.util.find_spec(m) for m in ('webview', 'pynput')))"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        if check.returncode:
            return False
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = os.environ.copy()
        env["ETU_COMPANION_PID_FILE"] = str(companion_pid_file(ROOT_DIR))
        subprocess.Popen([python, str(ROOT_DIR / "companion.py")], cwd=str(ROOT_DIR),
                         stdin=subprocess.DEVNULL, creationflags=flags, start_new_session=True, env=env)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Démarre ETU-AI avec un profil réseau explicite.")
    parser.add_argument("--mode", choices=LAUNCH_MODES, help="normal (défaut), local ou server")
    parser.add_argument("--downloads", action="store_true", help="ouvre le suivi des téléchargements")
    args = parser.parse_args(argv)
    mode = args.mode or saved_launch_mode()
    browser_url = SERVER_URL + ("/#/downloads" if args.downloads else "")

    print("=" * 60)
    print("           ETU-AI | Assistant Étudiant Autonome           ")
    print("=" * 60)
    print(f"[i] Mode sélectionné : {mode}")
    if mode == "server":
        print(f"[!] L'API MAIA Beacon sera accessible sans authentification depuis les appareils pouvant joindre le port {beacon_port()}.")

    if check_server_ready(SERVER_URL, timeout=1):
        print("[OK] ETU-AI est déjà lancé. Ouverture du navigateur.")
        webbrowser.open(browser_url)
        return 0

    # 1. Démarrer le serveur
    try:
        process = start_server(mode)
    except RuntimeError as exc:
        print(f"[!] {exc}")
        return 1

    try:
        # 2. Attendre que l'API soit prête
        print("[+] Attente de la disponibilité de l'interface...")
        if check_server_ready(SERVER_URL, timeout=20):
            print(f"[OK] Serveur prêt ! Ouverture du navigateur sur {SERVER_URL}")
            if not start_companion():
                print("[i] Fenêtre compagnon indisponible : installez requirements-desktop.txt dans le venv.")
            if mode == "server":
                lan_ips = sorted({
                    address[4][0]
                    for address in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                    if ipaddress.ip_address(address[4][0]).is_private
                    and not ipaddress.ip_address(address[4][0]).is_loopback
                })
                print(f"[!] MAIA Beacon expose son API sans authentification au réseau local, port {beacon_port()}.")
                for address in lan_ips:
                    print(f"    http://{address}:{beacon_port()}")
            webbrowser.open(browser_url)
        else:
            print("[!] Le serveur met plus de temps que prévu à démarrer.")
            print(f"    Vous pouvez ouvrir manuellement : {SERVER_URL}")

        print("[i] L'application est active. Appuyez sur Ctrl+C pour arrêter le serveur.")
        return process.wait()
    except KeyboardInterrupt:
        print("\n[+] Arrêt de l'application ETU-AI...")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        return 0


if __name__ == "__main__":
    sys.exit(main())
