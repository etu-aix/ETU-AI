"""
Gestionnaire asynchrone de téléchargement de modèles GGUF depuis Hugging Face.
Fournit un suivi de progression en temps réel pour l'interface utilisateur.
"""

import hashlib
import http.client
import os
from pathlib import Path
import re
import socket
import threading
import time
from typing import Any, Optional
import urllib.request
import uuid

from storage.db import StorageManager
from models_mgr.projectors import is_projector

DEFAULT_MODELS_DIR = Path(__file__).resolve().parent.parent / "ai-server" / "models"


def _connect_download(address, timeout=30, source_address=None):
    """Try IPv4 first: unreachable IPv6 routes must not stall each CDN request."""
    addresses = socket.getaddrinfo(*address, 0, socket.SOCK_STREAM)
    addresses.sort(key=lambda item: item[0] != socket.AF_INET)
    deadline = time.monotonic() + timeout
    error = OSError("Aucune adresse réseau disponible pour le téléchargement")
    for family, kind, protocol, _, endpoint in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(min(remaining, 3))
            if source_address:
                connection.bind(source_address)
            connection.connect(endpoint)
            connection.settimeout(timeout)
            return connection
        except OSError as exc:
            error = exc
            connection.close()
    raise error


class _DownloadHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_download


class _DownloadHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_DownloadHTTPSConnection, request, context=self._context)


def open_download(request, timeout=30):
    # Keep urllib's proxy, redirect and certificate handling local to this transfer.
    return urllib.request.build_opener(_DownloadHTTPSHandler()).open(request, timeout=timeout)


class DownloadTask:
    """Représente une tâche active de téléchargement avec métriques en temps réel."""

    def __init__(self, task_id: str, repo: str, filename: str, target_dir: Path, expected_sha256: Optional[str] = None, local_filename: Optional[str] = None):
        self.task_id = task_id
        self.repo = repo
        self.filename = filename
        self.local_filename = local_filename or filename
        self.target_dir = target_dir
        self.status = "queued"  # queued, downloading, completed, cancelled, error
        self.cancel_event = threading.Event()
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.percentage = 0.0
        self.speed_mbps = 0.0
        self.error_message: Optional[str] = None
        self.final_path: Optional[Path] = None
        self.sha256: Optional[str] = None
        self.expected_sha256 = expected_sha256

    def to_dict(self) -> dict[str, Any]:
        """Convertit l'état de la tâche en dictionnaire sérialisable."""
        return {
            "task_id": self.task_id,
            "repo": self.repo,
            "filename": self.filename,
            "local_filename": self.local_filename,
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

    @staticmethod
    def validate_reference(repo: str, filename: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.gguf", filename, re.IGNORECASE):
            raise ValueError("Dépôt ou nom de fichier GGUF invalide")

    def start_download(self, repo: str, filename: str, expected_sha256: Optional[str] = None, local_filename: Optional[str] = None) -> str:
        """Démarre le téléchargement d'un modèle et renvoie son identifiant de tâche."""
        self.validate_reference(repo, filename)
        if local_filename:
            self.validate_reference(repo, local_filename)
        task_id = str(uuid.uuid4())
        if expected_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise ValueError("Empreinte SHA256 invalide")
        task = DownloadTask(task_id, repo, filename, self.models_dir, expected_sha256, local_filename)

        with self._lock:
            for active in self.active_tasks.values():
                if active.local_filename == task.local_filename and active.status in ("queued", "downloading"):
                    if active.repo == repo:
                        return active.task_id
                    raise ValueError("Un autre dépôt télécharge déjà ce nom de fichier")
            self.active_tasks[task_id] = task

        thread = threading.Thread(target=self._run_download, args=(task,), daemon=True)
        thread.start()
        return task_id

    def cancel_download(self, task_id: str) -> bool:
        with self._lock:
            task = self.active_tasks.get(task_id)
            if not task or task.status not in ("queued", "downloading"):
                return False
            task.cancel_event.set()
            return True

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
        return [f.name for f in self.models_dir.glob("*.gguf") if f.is_file() and not is_projector(f.name)]

    def list_installed_projectors(self) -> list[str]:
        return [f.name for f in self.models_dir.glob("*.gguf") if f.is_file() and is_projector(f.name)]

    def _run_download(self, task: DownloadTask) -> None:
        """Télécharge dans un fichier partiel réutilisable après interruption."""
        task.status = "downloading"
        url = f"https://huggingface.co/{task.repo}/resolve/main/{task.filename}"
        dest_file = self.models_dir / task.local_filename
        repo_tag = hashlib.sha256(task.repo.encode("utf-8")).hexdigest()[:12]
        partial_file = self.models_dir / f"{task.local_filename}.{repo_tag}.part"

        try:
            offset = partial_file.stat().st_size if partial_file.exists() else 0
            headers = {"User-Agent": "ETU-AI-Downloader/1.0"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            req = urllib.request.Request(url, headers=headers)
            with open_download(req, timeout=30) as resp:
                response_code = getattr(resp, "status", 200)
                content_range = resp.headers.get("Content-Range", "")
                if response_code == 206:
                    bounds = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                    if not bounds or int(bounds[1]) != offset or int(bounds[2]) != int(bounds[3]) - 1:
                        raise IOError("Réponse partielle inattendue de Hugging Face ; réessayez le téléchargement")
                resume = offset > 0 and response_code == 206 and content_range.startswith(f"bytes {offset}-")
                if not resume:
                    offset = 0
                content_length = resp.headers.get("Content-Length")
                task.total_bytes = offset + int(content_length) if content_length else 0
                task.downloaded_bytes = offset

                hasher = hashlib.sha256()
                if resume:
                    with open(partial_file, "rb") as existing:
                        for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                            hasher.update(chunk)
                with open(partial_file, "ab" if resume else "wb") as out:
                    last_check_time = time.monotonic()
                    bytes_since_check = 0
                    while True:
                        if task.cancel_event.is_set():
                            task.status = "cancelled"
                            return
                        chunk = resp.read(1024 * 1024)
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
                        if task.total_bytes:
                            task.percentage = min(100.0, task.downloaded_bytes / task.total_bytes * 100)

                if task.cancel_event.is_set():
                    task.status = "cancelled"
                    return
                if task.total_bytes and task.downloaded_bytes != task.total_bytes:
                    raise IOError("Téléchargement incomplet ; la reprise sera proposée au prochain essai")
                digest = hasher.hexdigest()
                if task.expected_sha256 and digest.lower() != task.expected_sha256.lower():
                    partial_file.unlink(missing_ok=True)
                    raise IOError("Empreinte SHA256 du modèle différente de celle publiée par Hugging Face")
                os.replace(partial_file, dest_file)
                task.final_path = dest_file
                task.sha256 = digest
                task.status = "completed"
                task.percentage = 100.0
                self.storage.register_model(
                    filename=task.local_filename,
                    repo=task.repo,
                    size_bytes=task.downloaded_bytes,
                    sha256=task.sha256,
                )

        except Exception as e:
            task.status = "error"
            task.error_message = str(e)
