"""
Lanceur 1-Clic pour ETU-AI.
Démarre le serveur local en arrière-plan, vérifie sa disponibilité et ouvre automatiquement
l'interface web dans le navigateur par défaut de l'étudiant.
Compilable en .exe avec PyInstaller.
"""

import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT_DIR = Path(__file__).resolve().parent
SERVER_URL = "http://127.0.0.1:11343"


def check_server_ready(url: str, timeout: int = 15) -> bool:
    """Vérifie si le serveur local répond aux requêtes HTTP."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            with urllib.request.urlopen(f"{url}/api/status", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def start_server() -> subprocess.Popen:
    """Démarre le serveur API FastAPI local."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT_DIR) + os.pathsep + env.get("PYTHONPATH", "")

    # Lancement d'Uvicorn hébergeant api_server:app
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "api_server:app",
        "--host",
        "127.0.0.1",
        "--port",
        "11343",
        "--log-level",
        "info",
    ]

    print("[+] Lancement du serveur ETU-AI sur http://127.0.0.1:11343...")
    process = subprocess.Popen(
        cmd,
        cwd=str(ROOT_DIR),
        env=env,
    )
    return process


def main() -> int:
    print("=" * 60)
    print("           ETU-AI | Assistant Étudiant Autonome           ")
    print("=" * 60)

    # 1. Démarrer le serveur
    process = start_server()

    try:
        # 2. Attendre que l'API soit prête
        print("[+] Attente de la disponibilité de l'interface...")
        if check_server_ready(SERVER_URL, timeout=20):
            print(f"[OK] Serveur prêt ! Ouverture du navigateur sur {SERVER_URL}")
            webbrowser.open(SERVER_URL)
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
