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

ROOT_DIR = Path(__file__).resolve().parent
SERVER_URL = "http://127.0.0.1:8900"
BRIDGE_URL = "http://127.0.0.1:11346"
LAUNCH_MODES = ("normal", "local", "server")


def choose_launch_mode() -> str | None:
    """Show a small mode picker; normal mode is selected by default."""
    try:
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.title("Mode de lancement ETU-AI")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        mode = tk.StringVar(value="normal")
        result: dict[str, str | None] = {"mode": None}
        frame = ttk.Frame(root, padding=18)
        frame.grid(sticky="nsew")
        ttk.Label(frame, text="Choisissez le mode de lancement", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 12))
        options = (
            ("normal", "Normal — protections standard, services accessibles uniquement sur cet ordinateur."),
            ("local", "Local — autorise les origines ETU 2 sur localhost ; services limités à cet ordinateur."),
            ("server", "Serveur — expose MAIA Beacon sur le réseau local au port 11343."),
        )
        for row, (value, label) in enumerate(options, start=1):
            ttk.Radiobutton(frame, text=label, value=value, variable=mode).grid(row=row, column=0, sticky="w", pady=4)
        warning = ttk.Label(
            frame,
            text="Attention : l’API Beacon n’a pas d’authentification. Les appareils pouvant joindre votre réseau local pourront l’utiliser.",
            wraplength=510,
            foreground="#9a3412",
        )
        warning.grid(row=4, column=0, sticky="w", pady=(8, 10))
        warning.grid_remove()

        def update_warning(*_args: object) -> None:
            if mode.get() == "server":
                warning.grid()
            else:
                warning.grid_remove()

        mode.trace_add("write", update_warning)
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, sticky="e")
        ttk.Button(buttons, text="Annuler", command=root.destroy).grid(row=0, column=0, padx=(0, 8))

        def launch() -> None:
            result["mode"] = mode.get()
            root.destroy()

        ttk.Button(buttons, text="Lancer ETU-AI", command=launch).grid(row=0, column=1)
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        root.mainloop()
        return result["mode"]
    except Exception as exc:
        print(f"[!] Sélecteur graphique indisponible ({exc}). Le mode normal sera utilisé.")
        return "normal"


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


def start_server(mode: str = "normal") -> subprocess.Popen:
    """Démarre le serveur API FastAPI local."""
    env = configure_launch_environment(os.environ, mode)
    env["PYTHONPATH"] = str(ROOT_DIR) + os.pathsep + env.get("PYTHONPATH", "")

    # One process serves both the UI and ETU 2 bridge, sharing pairing state.
    cmd = [
        sys.executable,
        str(ROOT_DIR / "serve.py"),
    ]

    print("[+] Lancement du serveur ETU-AI sur http://127.0.0.1:8900...")
    process = subprocess.Popen(
        cmd,
        cwd=str(ROOT_DIR),
        env=env,
    )
    return process


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Démarre ETU-AI avec un profil réseau explicite.")
    parser.add_argument("--mode", choices=LAUNCH_MODES, help="normal (défaut), local ou server")
    args = parser.parse_args(argv)
    mode = args.mode or choose_launch_mode()
    if mode is None:
        print("[i] Lancement annulé.")
        return 0

    print("=" * 60)
    print("           ETU-AI | Assistant Étudiant Autonome           ")
    print("=" * 60)
    print(f"[i] Mode sélectionné : {mode}")
    if mode == "server":
        print("[!] L'API MAIA Beacon sera accessible sans authentification depuis les appareils pouvant joindre le port 11343.")

    if check_server_ready(SERVER_URL, timeout=1):
        try:
            with urllib.request.urlopen(f"{SERVER_URL}/api/launch-mode", timeout=2) as response:
                running_mode = json.load(response).get("mode")
        except Exception:
            running_mode = None
        if running_mode == mode:
            print("[OK] ETU-AI est déjà lancé dans ce mode. Ouverture du navigateur.")
            webbrowser.open(SERVER_URL)
            return 0
        print(f"[!] ETU-AI répond déjà en mode {running_mode or 'inconnu'} ; arrêt manuel requis avant de passer en mode {mode}.")
        return 1

    # 1. Démarrer le serveur
    process = start_server(mode)

    try:
        # 2. Attendre que l'API soit prête
        print("[+] Attente de la disponibilité de l'interface...")
        if check_server_ready(SERVER_URL, timeout=20):
            print(f"[OK] Serveur prêt ! Ouverture du navigateur sur {SERVER_URL}")
            if mode == "server":
                lan_ips = sorted({
                    address[4][0]
                    for address in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                    if ipaddress.ip_address(address[4][0]).is_private
                    and not ipaddress.ip_address(address[4][0]).is_loopback
                })
                print("[!] MAIA Beacon expose son API sans authentification au réseau local, port 11343.")
                for address in lan_ips:
                    print(f"    http://{address}:11343")
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
