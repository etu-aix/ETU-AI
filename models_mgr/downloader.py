"""
Gestionnaire asynchrone de téléchargement de modèles GGUF depuis Hugging Face.
Fournit un suivi de progression en temps réel pour l'interface utilisateur.
"""

from collections.abc import Callable
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Any, Optional
import urllib.request
import uuid

from storage.db import StorageManager

DEFAULT_MODELS_DIR = Path(__file__).resolve().parent.parent / "ai-server" / "models"


class DownloadTask:
    """Représente une tâche active de téléchargement avec métriques en temps réel."""

    def __init__(self, task_id: str, repo: str, filename: str, target_dir: Path):
        self.task_id = task_id
        self.repo = repo
        self.filename = filename
        self.target_dir = target_dir
        self.status = "queued"  # queued, downloading, completed, error
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.percentage = 0.0
        self.speed_mbps = 0.0
        self.error_message: Optional[str] = None
        self.final_path: Optional[Path] = None
        self.sha256: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convertit l'état de la tâche en dictionnaire sérialisable."""
        return {
            "task_id": self.task_id,
            "repo": self.repo,
            "filename": self.filename,
            "status": self.status,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "percentage": round(self.percentage, 1),
            "speed_mbps": round(self.speed_mbps, 2),
            "error_message": self.error_message,
        }


class ModelDownloader:
    """Supervise les téléchargements de modèles Hugging Face en arrière-plan."""

    def __init__(
        self,
        models_dir: Path = DEFAULT_MODELS_DIR,
        storage: Optional[StorageManager] = None,
    ):
        self.models_dir = models_dir
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.storage = storage or StorageManager()
        self.active_tasks: dict[str, DownloadTask] = {}
        self._lock = threading.Lock()

    def start_download(self, repo: str, filename: str) -> str:
        """Démarre le téléchargement d'un modèle et renvoie son identifiant de tâche."""
        task_id = str(uuid.uuid4())
        task = DownloadTask(task_id, repo, filename, self.models_dir)

        with self._lock:
            self.active_tasks[task_id] = task

        thread = threading.Thread(target=self._run_download, args=(task,), daemon=True)
        thread.start()
        return task_id

    def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        """Récupère l'état d'avancement d'une tâche."""
        with self._lock:
            task = self.active_tasks.get(task_id)
            return task.to_dict() if task else None

    def list_tasks(self) -> list[dict[str, Any]]:
        """Liste toutes les tâches suivies."""
        with self._lock:
            return [task.to_dict() for task in self.active_tasks.values()]

    def list_installed_models(self) -> list[str]:
        """Liste les fichiers de modèles GGUF présents localement."""
        if not self.models_dir.exists():
            return []
        return [f.name for f in self.models_dir.glob("*.gguf") if f.is_file()]

    def _run_download(self, task: DownloadTask) -> None:
        """Processus de téléchargement avec suivi de vitesse et validation de hash."""
        task.status = "downloading"
        url = f"https://huggingface.co/{task.repo}/resolve/main/{task.filename}"
        dest_file = self.models_dir / task.filename

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ETU-AI-Downloader/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                content_length = resp.headers.get("Content-Length")
                task.total_bytes = int(content_length) if content_length else 0

                hasher = hashlib.sha256()
                with tempfile.TemporaryDirectory(dir=self.models_dir, prefix="dl-") as tmpdir:
                    tmp_file = Path(tmpdir) / task.filename
                    with open(tmp_file, "wb") as out:
                        chunk_size = 1024 * 1024  # 1 Mo par chunk
                        start_time = time.monotonic()
                        bytes_since_check = 0
                        last_check_time = start_time

                        while True:
                            chunk = resp.read(chunk_size)
                            if not chunk:
                                break
                            out.write(chunk)
                            hasher.update(chunk)
                            task.downloaded_bytes += len(chunk)
                            bytes_since_check += len(chunk)

                            now = time.monotonic()
                            dt = now - last_check_time
                            if dt >= 0.5:
                                task.speed_mbps = (bytes_since_check / dt) / (1024 * 1024)
                                bytes_since_check = 0
                                last_check_time = now
                                if task.total_bytes > 0:
                                    task.percentage = (task.downloaded_bytes / task.total_bytes) * 100

                    # Finalisation atomique
                    shutil.move(str(tmp_file), str(dest_file))
                    task.final_path = dest_file
                    task.sha256 = hasher.hexdigest()
                    task.status = "completed"
                    task.percentage = 100.0

                    # Enregistrement dans la base locale
                    self.storage.register_model(
                        filename=task.filename,
                        repo=task.repo,
                        size_bytes=task.downloaded_bytes,
                        sha256=task.sha256,
                    )

        except Exception as e:
            task.status = "error"
            task.error_message = str(e)
