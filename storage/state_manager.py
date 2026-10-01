"""
Module de gestion d'état centralisé pour ETU-AI.
Gère la persistance de l'état global du système (ce qui a été installé,
statut du setup, modèles présents, configuration matérielle, etc.)
dans un fichier JSON dédié (storage/app_state.json) et synchronisé avec SQLite.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any, Optional

STATE_FILE = Path(__file__).resolve().parent / "app_state.json"


class StateManager:
    """Gestionnaire central de l'état persistant de l'application ETU-AI."""

    _instance = None
    _lock = threading.RLock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(self, file_path: Path = STATE_FILE):
        if hasattr(self, "_initialized") and self._initialized:
            return
        self.file_path = file_path
        self._rw_lock = threading.RLock()
        self._initialized = True
        self._load_or_init()

    def _default_state(self) -> dict[str, Any]:
        """Génère l'état par défaut d'une nouvelle installation."""
        return {
            "app_version": "1.0.0",
            "setup": {
                "completed": False,
                "completed_at": None,
                "version": "1.0.0",
                "last_step": 0,
                "history": [],
            },
            "hardware": {
                "cpu": None,
                "gpus": [],
                "npus": [],
                "has_npu": False,
                "recommended_backend": "cpu",
            },
            "installed_components": {
                "llama_server": {
                    "installed": False,
                    "status": "pending",
                    "path": None,
                    "backend": None,
                },
                "beacon": {
                    "installed": False,
                    "status": "pending",
                    "port": 11343,
                    "auto_start": True,
                },
            },
            "installed_models": [],
            "active_model": None,
            "preferences": {
                "auto_start_beacon": True,
                "dark_mode": True,
                "theme_ambient_glow": True,
                "particles_intensity": "high",
            },
            "system_info": {
                "os": os.name,
                "initialized_at": datetime.now(timezone.utc).isoformat(),
                "last_startup": datetime.now(timezone.utc).isoformat(),
            },
        }

    def _load_or_init(self) -> None:
        """Charge l'état depuis le fichier JSON ou le crée s'il n'existe pas."""
        with self._rw_lock:
            if not self.file_path.exists():
                state = self._default_state()
                self._save_atomic(state)
            else:
                try:
                    with open(self.file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        # Assurer la présence des clés essentielles
                        default = self._default_state()
                        for k, v in default.items():
                            if k not in data:
                                data[k] = v
                        self._save_atomic(data)
                except Exception:
                    # En cas de corruption, réinitialiser proprement
                    state = self._default_state()
                    self._save_atomic(state)

    def _save_atomic(self, data: dict[str, Any]) -> None:
        """Sauvegarde atomique du fichier JSON pour éviter toute corruption."""
        tmp_file = self.file_path.with_suffix(".tmp")
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp_file.replace(self.file_path)

    def get_state(self) -> dict[str, Any]:
        """Retourne une copie fraîche de l'état complet de l'application."""
        with self._rw_lock:
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return self._default_state()

    def update_state(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Met à jour des sections spécifiques de l'état persistant."""
        with self._rw_lock:
            state = self.get_state()

            def deep_update(d, u):
                for k, v in u.items():
                    if isinstance(v, dict) and k in d and isinstance(d[k], dict):
                        deep_update(d[k], v)
                    else:
                        d[k] = v

            deep_update(state, updates)
            self._save_atomic(state)
            return state

    def is_setup_completed(self) -> bool:
        """Vérifie si le setup a été marqué comme complété."""
        state = self.get_state()
        return bool(state.get("setup", {}).get("completed", False))

    def mark_setup_completed(
        self,
        primary_model: Optional[str] = None,
        installed_models: Optional[list[dict[str, Any]]] = None,
        hardware_info: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Marque définitivement le setup comme terminé avec l'état complet."""
        now = datetime.now(timezone.utc).isoformat()
        state = self.get_state()

        state["setup"]["completed"] = True
        state["setup"]["completed_at"] = now
        state["setup"]["last_step"] = 4
        state["setup"]["history"].append({
            "event": "setup_completed",
            "timestamp": now,
        })

        if hardware_info:
            state["hardware"] = hardware_info

        state["installed_components"]["llama_server"]["installed"] = True
        state["installed_components"]["llama_server"]["status"] = "ready"
        state["installed_components"]["beacon"]["installed"] = True
        state["installed_components"]["beacon"]["status"] = "online"

        if installed_models:
            existing = {m.get("filename"): m for m in state.get("installed_models", [])}
            for m in installed_models:
                fn = m.get("filename")
                if fn:
                    existing[fn] = {
                        "filename": fn,
                        "repo": m.get("repo", "local"),
                        "size_bytes": m.get("size_bytes", 0),
                        "installed_at": m.get("installed_at", now),
                        "npu_compatible": m.get("npu_compatible", False),
                    }
            state["installed_models"] = list(existing.values())

        if primary_model:
            state["active_model"] = primary_model

        with self._rw_lock:
            self._save_atomic(state)
        return state

    def record_installed_model(
        self,
        filename: str,
        repo: str,
        size_bytes: int = 0,
        npu_compatible: bool = False,
        quant: Optional[str] = None,
    ) -> None:
        """Enregistre un modèle installé dans l'état de l'application."""
        now = datetime.now(timezone.utc).isoformat()
        state = self.get_state()
        models = state.get("installed_models", [])

        # Évite les doublons
        filtered = [m for m in models if m.get("filename") != filename]
        filtered.append({
            "filename": filename,
            "repo": repo,
            "size_bytes": size_bytes,
            "size_gib": round(size_bytes / (1024 ** 3), 2) if size_bytes else 0,
            "quant": quant,
            "npu_compatible": npu_compatible,
            "installed_at": now,
        })
        state["installed_models"] = filtered

        if not state.get("active_model"):
            state["active_model"] = filename

        with self._rw_lock:
            self._save_atomic(state)

    def record_startup(self) -> None:
        """Enregistre l'horodatage du démarrage de l'application."""
        now = datetime.now(timezone.utc).isoformat()
        self.update_state({
            "system_info": {
                "last_startup": now,
            }
        })

    def reset_setup(self) -> None:
        """Réinitialise le statut du setup pour permettre une reconfiguration."""
        state = self.get_state()
        state["setup"]["completed"] = False
        state["setup"]["last_step"] = 0
        state["setup"]["history"].append({
            "event": "setup_reset",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        with self._rw_lock:
            self._save_atomic(state)


# Instance globale partagée
state_manager = StateManager()
