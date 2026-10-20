"""Native HF exports: variants and complete, resumable local installations."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import uuid

from models_mgr.downloader import DownloadTask, ModelDownloader


def safe_path(value):
    parts = value.split("/") if isinstance(value, str) else []
    if not parts or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."}
                        or part.endswith((".", " ")) or part.split(".")[0].upper() in
                        {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
                        for part in parts):
        raise ValueError("Chemin de fichier du modèle invalide")
    return PurePosixPath(value)


def quant_label(value):
    match = re.search(r"(?:^|[/_.-])(int[248]|uint[248]|fp(?:16|32)|bf16|w[248](?:a\d+)?|q[248])(?=$|[/_.-])", value, re.I)
    if not match:
        return "Précision non indiquée"
    label = match[1].upper()
    return {"W4": "INT4", "W8": "INT8", "Q4": "INT4", "Q8": "INT8"}.get(label, label)


def bundle_id(repo, filename):
    return "native-" + hashlib.sha256(f"{repo}/{filename}".encode()).hexdigest()[:24]


def native_variants(repo, info, formats):
    files = {}
    for entry in info.get("siblings", []):
        path = entry.get("rfilename", "")
        try:
            safe_path(path)
        except ValueError:
            continue
        files[path] = entry
    graphs = []
    for path in files:
        name = PurePosixPath(path).name.lower()
        if "tokenizer" in name:
            continue
        if "openvino" in formats and path.endswith(".xml") and path[:-4] + ".bin" in files:
            graphs.append(("openvino", path))
        if "onnx" in formats and path.endswith(".onnx"):
            graphs.append(("onnx", path))
    # A split decoder or a vision encoder is part of the same export, not a
    # quantization option. Group graphs by directory and published precision.
    graph_groups = {}
    for fmt, graph in sorted(graphs, key=lambda item: PurePosixPath(item[1]).name not in {"openvino_model.xml", "model.onnx"}):
        key = (fmt, str(PurePosixPath(graph).parent), quant_label(graph))
        graph_groups.setdefault(key, []).append(graph)
    variants = []
    for (fmt, _, _), model_graphs in graph_groups.items():
        graph = model_graphs[0]
        folder = str(PurePosixPath(graph).parent)
        same_folder = lambda path: str(PurePosixPath(path).parent) == folder
        # Keep configuration/tokenizers from the root and the selected export's
        # folder. Never fetch source code or another precision's model weights.
        selected = []
        other_stems = [str(PurePosixPath(path).with_suffix("")) for other_fmt, path in graphs
                       if other_fmt == fmt and path not in model_graphs and same_folder(path)]
        for path, entry in files.items():
            name = PurePosixPath(path).name.lower()
            suffix = PurePosixPath(path).suffix.lower()
            local = same_folder(path)
            root = "/" not in path
            config = suffix in {".json", ".txt", ".model", ".tiktoken", ".vocab", ".merges", ".jinja", ".jinja2", ".spm"}
            tokenizer = "tokenizer" in name and suffix in {".xml", ".bin", ".onnx"}
            weights = (fmt == "openvino" and any(path in {model_graph, model_graph[:-4] + ".bin"} for model_graph in model_graphs)) or (
                fmt == "onnx" and (path in model_graphs or
                (suffix in {".data", ".onnx_data", ".bin", ".pb"} or ".data." in name)
                and not any(path.startswith(stem + ".") or path.startswith(stem + "_") for stem in other_stems)))
            if ((root or local) and (config or tokenizer)) or (local and weights):
                size = entry.get("size") or (entry.get("lfs") or {}).get("size") or 0
                selected.append({"filename": path, "size_bytes": size, "sha256": (entry.get("lfs") or {}).get("oid")})
        size = sum(item["size_bytes"] for item in selected)
        label = quant_label(graph)
        if label == "Précision non indiquée":
            label = quant_label(repo)
        variants.append({"id": bundle_id(repo, graph), "repo": repo, "format": fmt,
                         "filename": graph, "quant": label, "files": selected,
                         "size_gib": round(size / 1024 ** 3, 2), "size_bytes": size})
    return sorted(variants, key=lambda item: (PurePosixPath(item["filename"]).name not in {"openvino_model.xml", "model.onnx"}, item["filename"]))


class BundleTask(DownloadTask):
    def __init__(self, repo, variant, target):
        super().__init__(str(uuid.uuid4()), repo, variant["filename"], target)
        self.variant = variant
        self.local_filename = variant["id"]
        self.child = None
        self.completed_bytes = 0
        self.total_bytes = variant["size_bytes"]

    def to_dict(self):
        result = super().to_dict()
        downloaded = self.completed_bytes + (self.child.downloaded_bytes if self.child else 0)
        result.update(format=self.variant["format"], quant=self.variant["quant"],
                      filename=f'{self.repo.split("/", 1)[1]} · {self.variant["quant"]}',
                      downloaded_bytes=downloaded,
                      percentage=round(min(100, downloaded / self.total_bytes * 100), 1) if self.total_bytes else 0,
                      speed_mbps=self.child.speed_mbps if self.child else 0)
        if self.status == "completed":
            result["percentage"] = 100
        return result


class _NoRegistration:
    def register_model(self, **kwargs):
        pass


class NativeInstaller:
    def __init__(self, downloader):
        self.downloader = downloader
        self.root = downloader.models_dir / "native"

    def _target(self, model_id):
        if not re.fullmatch(r"native-[a-f0-9]{24}", model_id):
            raise ValueError("Identifiant de modèle natif invalide")
        target = self.root / model_id
        if self.root.is_symlink() or target.is_symlink() or not target.resolve().is_relative_to(self.downloader.models_dir.resolve()):
            raise ValueError("Dossier de modèle natif invalide")
        return target

    def installed(self):
        results = []
        for marker in self.root.glob("native-*/installation.json"):
            try:
                target = self._target(marker.parent.name)
                info = json.loads(marker.read_text(encoding="utf-8"))
                if marker.is_symlink() or info.get("id") != target.name:
                    continue
                if all((target / str(safe_path(entry["filename"]))).is_file() for entry in info["files"]):
                    results.append(info)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return results

    def start(self, repo, variant):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("Dépôt invalide")
        if not variant.get("files") or variant["format"] not in {"openvino", "onnx"}:
            raise ValueError("Export natif incomplet")
        target = self._target(variant["id"])
        for entry in variant["files"]:
            path = target / str(safe_path(entry["filename"]))
            if not path.resolve().is_relative_to(target.resolve()) or path.is_symlink():
                raise ValueError("Chemin de modèle invalide")
        task = BundleTask(repo, variant, target)
        with self.downloader._lock:
            for active in self.downloader.active_tasks.values():
                if active.local_filename == task.local_filename and active.status in {"queued", "downloading"}:
                    return active.task_id
            self.downloader.active_tasks[task.task_id] = task
        threading.Thread(target=self._run, args=(task,), daemon=True).start()
        return task.task_id

    def _run(self, task):
        task.status = "downloading"
        try:
            target = self._target(task.variant["id"])
            target.mkdir(parents=True, exist_ok=True)
            for entry in task.variant["files"]:
                if task.cancel_event.is_set():
                    task.status = "cancelled"
                    return
                destination = target / str(safe_path(entry["filename"]))
                if destination.is_symlink() or not destination.resolve().is_relative_to(target.resolve()):
                    raise ValueError("Chemin de modèle invalide")
                destination.parent.mkdir(parents=True, exist_ok=True)
                checksum = entry.get("sha256")
                if destination.is_file() and entry["size_bytes"] > 0 and destination.stat().st_size == entry["size_bytes"] and checksum:
                    with destination.open("rb") as stream:
                        valid = hashlib.file_digest(stream, "sha256").hexdigest() == checksum
                    if valid:
                        task.completed_bytes += destination.stat().st_size
                        continue
                child = DownloadTask(task.task_id, task.repo, entry["filename"], destination.parent,
                                     checksum, destination.name)
                child.cancel_event = task.cancel_event
                task.child = child
                worker = ModelDownloader(destination.parent, _NoRegistration())
                worker._run_download(child)
                if child.status != "completed":
                    task.status, task.error_message = child.status, child.error_message
                    return
                if entry["size_bytes"] and destination.stat().st_size != entry["size_bytes"]:
                    raise IOError("Taille du fichier différente de celle publiée par Hugging Face")
                task.completed_bytes += child.downloaded_bytes
                task.child = None
            if task.cancel_event.is_set():
                task.status = "cancelled"
                return
            marker = target / "installation.json"
            temporary = target / "installation.json.tmp"
            temporary.write_text(json.dumps(task.variant, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, marker)
            task.status = "completed"
        except Exception as exc:
            task.status, task.error_message = "error", str(exc)

    def remove(self, model_id):
        target = self._target(model_id)
        with self.downloader._lock:
            if any(task.local_filename == model_id and task.status in {"queued", "downloading"}
                   for task in self.downloader.active_tasks.values()):
                raise ValueError("Annulez le téléchargement avant de désinstaller ce modèle")
            if target.exists():
                shutil.rmtree(target)
