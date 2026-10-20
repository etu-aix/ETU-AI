"""Associate vision projectors with exact model variants, never just a model family."""

import hashlib
import json
from pathlib import Path
import re
import threading
from uuid import uuid4

from models_mgr.catalog import RECOMMENDED_CATALOG

_ASSIGNMENT_LOCK = threading.RLock()


def is_projector(filename):
    return any(word in filename.lower() for word in ("mmproj", "projector"))


def model_identity(filename):
    stem = Path(filename).stem.lower()
    stem = re.sub(r"[-_.]\d{5}-of-\d{5}$", "", stem)
    stem = re.sub(r"^(?:mmproj|projector)[-_.](?:model[-_.])?", "", stem)
    stem = re.sub(r"(?:^|[-_.])(?:mmproj|projector)(?=$|[-_.])", "-", stem)
    stem = re.sub(r"(?:^|[-_.])(?:ud[-_.])?(?:bf16|f16|f32|fp16|fp32|q\d(?:_[a-z0-9]+)*|iq\d(?:_[a-z0-9]+)*)(?=$|[-.])", "", stem)
    return re.sub(r"[^a-z0-9]", "", stem)


def projector_alias(repo, filename):
    digest = hashlib.sha256(f"{repo}/{filename}".encode()).hexdigest()[:20]
    return f"mmproj-{digest}.gguf"


def known_text_only(model):
    if re.search(r"(?:^|[-_.])text[-_.]instruct(?:[-_.]|$)", Path(model).stem.lower()):
        return True
    identity = model_identity(model)
    return any(model_identity(item["filename"]) == identity and item["id"] != "ministral-3b" for item in RECOMMENDED_CATALOG)


def matching_projectors(model, files):
    """Generic mmproj names are safe only in repositories containing one model variant."""
    if known_text_only(model):
        return []
    identity = model_identity(model)
    # MTP drafters are auxiliary weights, not a second vision-model variant.
    identities = {
        model_identity(item["filename"]) for item in files
        if not is_projector(item["filename"])
        and not re.search(r"(?:^|[-_.])mtp(?:$|[-_.])", Path(item["filename"]).stem, re.IGNORECASE)
    }
    matches = []
    for item in files:
        if not is_projector(item["filename"]):
            continue
        candidate = model_identity(item["filename"])
        if candidate == identity or (not candidate and identities == {identity}):
            matches.append(item)
    return sorted(matches, key=lambda item: (0 if "f16" in item["filename"].lower() and "bf16" not in item["filename"].lower() else 1, item.get("size_bytes", 0), item["filename"]))


class ProjectorManager:
    def __init__(self, storage, downloader, hf_client):
        self.storage = storage
        self.downloader = downloader
        self.hf_client = hf_client

    def repo_for(self, model):
        for item in self.storage.list_cached_models():
            if item["filename"] == model:
                return item["repo"]
        for item in RECOMMENDED_CATALOG:
            if model_identity(item["filename"]) == model_identity(model):
                return item["repo"]
        association = self.association(model)
        if association and association.get("repo") != "local":
            return association.get("repo")
        return None

    def association(self, model):
        return (self.storage.get_setting("mmproj_assignments", {}) or {}).get(model)

    def describe(self, model):
        association = self.association(model)
        installed = bool(association and (self.downloader.models_dir / association["local_filename"]).is_file())
        return {"association": association, "installed": installed, "text_only": known_text_only(model), "repo": self.repo_for(model)}

    def discover(self, model, repo=None):
        repo = repo or self.repo_for(model)
        if known_text_only(model):
            return {**self.describe(model), "candidates": [], "message": "Ce modèle est textuel. Un projecteur ne lui ajoute pas la vision ; choisissez un modèle vision."}
        if not repo:
            return {**self.describe(model), "candidates": [], "message": "Indiquez le dépôt Hugging Face exact de ce modèle pour rechercher son projecteur."}
        self.downloader.validate_reference(repo, model)
        files = self.hf_client.get_model_files(repo)
        if not files:
            raise ConnectionError("Impossible de vérifier les fichiers du dépôt Hugging Face.")
        if not any(item["filename"] == model and not is_projector(model) for item in files):
            raise ValueError("Le modèle choisi n'est pas présent dans ce dépôt.")
        candidates = []
        for item in matching_projectors(model, files):
            local = projector_alias(repo, item["filename"])
            candidates.append({**item, "repo": repo, "local_filename": local, "is_installed": (self.downloader.models_dir / local).is_file()})
        return {**self.describe(model), "repo": repo, "candidates": candidates, "message": "Projecteurs correspondant à cette variante du modèle." if candidates else "Aucun projecteur dont la correspondance peut être vérifiée dans ce dépôt."}

    def assign(self, model, association):
        if model not in self.downloader.list_installed_models():
            raise ValueError("Le modèle doit être installé avant d'associer son projecteur.")
        with _ASSIGNMENT_LOCK:
            assignments = self.storage.get_setting("mmproj_assignments", {}) or {}
            if association is None:
                assignments.pop(model, None)
            else:
                assignments[model] = association
            self.storage.set_setting("mmproj_assignments", assignments)
            # The standalone Beacon launcher reads the same associations at startup.
            manifest = self.downloader.models_dir / ".etu-mmproj.json"
            temporary = manifest.with_suffix(f".{uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps(assignments, ensure_ascii=False), encoding="utf-8")
                temporary.replace(manifest)
            finally:
                temporary.unlink(missing_ok=True)

    def install(self, model, repo, filename):
        if model not in self.downloader.list_installed_models():
            raise ValueError("Le modèle doit être installé avant d'associer son projecteur.")
        result = self.discover(model, repo)
        candidate = next((item for item in result["candidates"] if item["filename"] == filename), None)
        if not candidate:
            raise ValueError("Ce projecteur ne correspond pas à cette variante du modèle.")
        association = {"repo": repo, "filename": filename, "local_filename": candidate["local_filename"], "manual": False}
        task_id = None
        if not candidate["is_installed"]:
            task_id = self.downloader.start_download(repo, filename, candidate.get("sha256"), local_filename=candidate["local_filename"])
        self.assign(model, association)
        return {"task_id": task_id, **self.describe(model)}

    def selected(self, model):
        association = self.association(model)
        if not association:
            return None
        filename = association["local_filename"]
        self.downloader.validate_reference("local/projectors", filename)
        path = self.downloader.models_dir / filename
        if not path.is_file():
            raise ValueError("Le projecteur associé doit être téléchargé ou importé avant d'activer le modèle.")
        return filename
