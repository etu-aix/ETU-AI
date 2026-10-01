"""
Orchestrateur du Setup Wizard pour ETU-AI.
Gère l'accueil, la sélection des modèles, le téléchargement, la préparation de MAIA-Beacon
et le démarrage supervisé du serveur d'IA sur le port 11343 avec retour d'état en direct.
"""

from collections.abc import Callable
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Optional
import urllib.error
import urllib.request

from storage.db import StorageManager
from storage.state_manager import state_manager

ROOT_DIR = Path(__file__).resolve().parent
AI_SERVER_DIR = ROOT_DIR / "ai-server"
BEACON_URL = "http://127.0.0.1:11343"


class SetupWizardManager:
    """Gère l'état global et l'exécution du processus d'installation initiale."""

    def __init__(self, storage: Optional[StorageManager] = None):
        self.storage = storage or StorageManager()
        self.state_mgr = state_manager
        self.lock = threading.Lock()
        self.status = "idle"  # idle, running, completed, error
        self.current_step_index = 0
        self.steps = [
            {"id": "llama", "title": "Diagnostic et moteur d'inférence (llama-server)", "status": "pending"},
            {"id": "beacon", "title": "Environnement superviseur (MAIA-Beacon)", "status": "pending"},
            {"id": "models", "title": "Téléchargement du ou des modèles sélectionnés", "status": "pending"},
            {"id": "service", "title": "Démarrage et validation de l'inférence (port 11343)", "status": "pending"},
        ]
        self.progress_percent = 0.0
        self.logs: list[str] = []
        self.error_message: Optional[str] = None
        self.beacon_process: Optional[subprocess.Popen] = None

    def is_completed(self) -> bool:
        """Vérifie si l'installation a déjà été effectuée avec succès."""
        # 1. Vérification dans l'état persistant StateManager
        if self.state_mgr.is_setup_completed():
            return True
        # 2. Vérification dans SQLite settings
        val = self.storage.get_setting("setup_completed", "false")
        if val in ("true", True, "1"):
            # Synchroniser StateManager
            self.state_mgr.update_state({"setup": {"completed": True}})
            return True
        return False

    def is_beacon_online(self) -> bool:
        """Vérifie si MAIA-Beacon est actif et répond sur le port 11343."""
        try:
            req = urllib.request.Request(f"{BEACON_URL}/v1/models")
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def ensure_beacon_running(self) -> bool:
        """Démarre le service MAIA-Beacon en arrière-plan s'il n'est pas déjà actif."""
        if self.is_beacon_online():
            return True

        with self.lock:
            if self.beacon_process and self.beacon_process.poll() is None:
                # Processus déjà en cours de lancement
                return True

            env = os.environ.copy()
            env["PYTHONPATH"] = str(AI_SERVER_DIR) + os.pathsep + env.get("PYTHONPATH", "")

            cmd_run = [sys.executable, "beacon.py"]
            try:
                self.beacon_process = subprocess.Popen(
                    cmd_run,
                    cwd=str(AI_SERVER_DIR),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )

                def _stream():
                    if self.beacon_process and self.beacon_process.stdout:
                        for l in self.beacon_process.stdout:
                            c = l.rstrip()
                            if c:
                                self.add_log(f"[Beacon] {c}")

                threading.Thread(target=_stream, daemon=True).start()
                return True
            except Exception as e:
                self.add_log(f"[!] Échec démarrage Beacon en arrière-plan: {e}")
                return False

    def get_status(self) -> dict[str, Any]:
        """Retourne l'état complet du setup pour le frontend."""
        with self.lock:
            return {
                "completed": self.is_completed(),
                "beacon_online": self.is_beacon_online(),
                "status": self.status,
                "current_step": self.current_step_index,
                "steps": self.steps,
                "progress_percent": round(self.progress_percent, 1),
                "logs": self.logs[-50:],  # 50 dernières lignes de logs
                "error_message": self.error_message,
                "app_state": self.state_mgr.get_state(),
            }

    def add_log(self, message: str) -> None:
        """Ajoute un message d'information horodaté aux logs."""
        timestamp = time.strftime("%H:%M:%S")
        entry = f"[{timestamp}] {message}"
        with self.lock:
            self.logs.append(entry)
            print(entry, flush=True)

    def start_setup(self, selected_models: list[dict[str, Any]]) -> bool:
        """Démarre le pipeline d'installation en arrière-plan."""
        with self.lock:
            if self.status == "running":
                return False
            self.status = "running"
            self.current_step_index = 0
            self.progress_percent = 5.0
            self.error_message = None
            self.logs.clear()
            for s in self.steps:
                s["status"] = "pending"

        thread = threading.Thread(target=self._run_pipeline, args=(selected_models,), daemon=True)
        thread.start()
        return True

    def _run_pipeline(self, selected_models: list[dict[str, Any]]) -> None:
        """Exécute les 4 étapes du setup séquentiellement avec streaming des logs."""
        try:
            primary_model = selected_models[0] if selected_models else {
                "repo": "mistralai/Ministral-3-3B-Instruct-2512-GGUF",
                "filename": "Ministral-3-3B-Instruct-2512-Q4_K_M.gguf",
            }

            self.add_log("Démarrage du processus d'installation d'ETU-AI...")

            # -----------------------------------------------------------------
            # Étape 1 & 2 : Préparation de llama-server et MAIA-Beacon
            # -----------------------------------------------------------------
            self._set_step_status(0, "running", progress=15.0)
            self.add_log(f"Configuration du modèle initial : {primary_model.get('filename')}")

            # Mise à jour de config.json dans ai-server avec le modèle choisi
            config_json_path = AI_SERVER_DIR / "config.json"
            if config_json_path.exists():
                try:
                    with open(config_json_path, "r", encoding="utf-8") as f:
                        cfg_data = json.load(f)
                    cfg_data["default_model_repo"] = primary_model.get("repo", "mistralai/Ministral-3-3B-Instruct-2512-GGUF")
                    cfg_data["default_model_file"] = primary_model.get("filename", "Ministral-3-3B-Instruct-2512-Q4_K_M.gguf")
                    with open(config_json_path, "w", encoding="utf-8") as f:
                        json.dump(cfg_data, f, indent=2)
                except Exception as e:
                    self.add_log(f"[!] Avertissement mise à jour config.json: {e}")

            # Lancement de beacon.py --setup-only pour préparer les binaires & venv
            self.add_log("Préparation des binaires et de l'environnement virtuel...")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(AI_SERVER_DIR) + os.pathsep + env.get("PYTHONPATH", "")

            cmd_setup = [sys.executable, "beacon.py", "--setup-only"]
            proc = subprocess.Popen(
                cmd_setup,
                cwd=str(AI_SERVER_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

            if proc.stdout:
                for line in proc.stdout:
                    clean = line.rstrip()
                    if clean:
                        self.add_log(clean)
                        if "llama-server" in clean.lower():
                            self._set_step_status(0, "running", progress=25.0)
                        if "beacon" in clean.lower():
                            self._set_step_status(1, "running", progress=40.0)

            ret = proc.wait()
            if ret != 0:
                raise RuntimeError(f"Échec de la préparation Beacon / llama-server (code {ret})")

            self._set_step_status(0, "completed")
            self._set_step_status(1, "completed", progress=50.0)
            self.add_log("[OK] Moteur llama-server et superviseur MAIA-Beacon prêts !")
            self.state_mgr.update_state({
                "installed_components": {
                    "llama_server": {"installed": True, "status": "ready", "path": str(AI_SERVER_DIR / "bin")},
                    "beacon": {"installed": True, "status": "configured", "port": 11343},
                }
            })

            # -----------------------------------------------------------------
            # Étape 3 : Téléchargement des modèles sélectionnés
            # -----------------------------------------------------------------
            self._set_step_status(2, "running", progress=55.0)
            from models_mgr.downloader import ModelDownloader

            downloader = ModelDownloader(storage=self.storage)
            for idx, model_info in enumerate(selected_models):
                repo = model_info.get("repo")
                filename = model_info.get("filename")
                if not repo or not filename:
                    continue

                dest_file = AI_SERVER_DIR / "models" / filename
                if dest_file.exists() and dest_file.stat().st_size > 1024 * 1024:
                    size_gib = round(dest_file.stat().st_size / (1024 ** 3), 2)
                    self.add_log(f"Modèle '{filename}' ({size_gib} Go) déjà présent localement. Téléchargement ignoré.")
                    self.storage.register_model(
                        filename=filename,
                        repo=repo if repo != "local" else "Modèle Local",
                        size_bytes=dest_file.stat().st_size,
                    )
                    self.state_mgr.record_installed_model(
                        filename=filename,
                        repo=repo,
                        size_bytes=dest_file.stat().st_size,
                        npu_compatible=bool(model_info.get("npu_compatible", False)),
                        quant=model_info.get("quant", "Q4_K_M"),
                    )
                    self._set_step_status(2, "running", progress=80.0)
                else:
                    self.add_log(f"Téléchargement du modèle {idx + 1}/{len(selected_models)} : {filename} depuis {repo}...")
                    task_id = downloader.start_download(repo, filename)
                    while True:
                        time.sleep(1)
                        t = downloader.get_task(task_id)
                        if not t:
                            break
                        if t["status"] == "downloading":
                            self.add_log(f"-> {filename} : {t['percentage']}% ({t['speed_mbps']} Mo/s)")
                            # Ajustement du progrès entre 55% et 80%
                            step_progress = 55.0 + (t["percentage"] * 0.25)
                            self._set_step_status(2, "running", progress=step_progress)
                        elif t["status"] == "completed":
                            self.add_log(f"[OK] {filename} téléchargé avec succès !")
                            self.state_mgr.record_installed_model(
                                filename=filename,
                                repo=repo,
                                size_bytes=dest_file.stat().st_size if dest_file.exists() else 0,
                                npu_compatible=bool(model_info.get("npu_compatible", False)),
                                quant=model_info.get("quant", "Q4_K_M"),
                            )
                            break
                        elif t["status"] == "error":
                            raise RuntimeError(f"Erreur téléchargement {filename}: {t.get('error_message')}")

            self._set_step_status(2, "completed", progress=85.0)

            # -----------------------------------------------------------------
            # Étape 4 : Démarrage du service Beacon en arrière-plan
            # -----------------------------------------------------------------
            self._set_step_status(3, "running", progress=90.0)
            self.add_log("Démarrage du service local MAIA-Beacon sur le port 11343...")

            cmd_run = [sys.executable, "beacon.py"]
            self.beacon_process = subprocess.Popen(
                cmd_run,
                cwd=str(AI_SERVER_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

            # Thread de lecture des logs de Beacon
            def stream_beacon_logs():
                if self.beacon_process and self.beacon_process.stdout:
                    for l in self.beacon_process.stdout:
                        clean_l = l.rstrip()
                        if clean_l:
                            self.add_log(f"[Beacon] {clean_l}")

            threading.Thread(target=stream_beacon_logs, daemon=True).start()

            # Attente de disponibilité de Beacon
            self.add_log("Validation de l'API Beacon et du chargement du modèle...")
            deadline = time.monotonic() + 120
            service_ready = False
            while time.monotonic() < deadline:
                if self.beacon_process.poll() is not None:
                    raise RuntimeError(f"Le processus Beacon s'est arrêté prématurément (code {self.beacon_process.returncode})")
                if self.is_beacon_online():
                    service_ready = True
                    break
                time.sleep(1)

            if not service_ready:
                raise RuntimeError("Délai d'attente dépassé pour la mise en ligne de MAIA-Beacon.")

            self._set_step_status(3, "completed", progress=100.0)
            self.add_log("[OK] MAIA-Beacon est en ligne sur http://127.0.0.1:11343 !")
            self.add_log("Installation d'ETU-AI terminée avec succès !")

            # Marquer le setup comme complété dans la base SQLite et dans StateManager
            self.storage.set_setting("setup_completed", "true")
            self.state_mgr.mark_setup_completed(
                primary_model=primary_model.get("filename"),
                installed_models=selected_models,
            )
            with self.lock:
                self.status = "completed"

        except Exception as e:
            with self.lock:
                self.status = "error"
                self.error_message = str(e)
            self.add_log(f"[ERREUR] Échec de l'installation : {e}")

    def _set_step_status(self, step_idx: int, status: str, progress: Optional[float] = None) -> None:
        """Met à jour l'état d'une étape et la jauge de progression."""
        with self.lock:
            self.current_step_index = step_idx
            if 0 <= step_idx < len(self.steps):
                self.steps[step_idx]["status"] = status
            if progress is not None:
                self.progress_percent = progress


# Instance globale partagée
wizard_manager = SetupWizardManager()
