"""
Serveur API FastAPI principal pour ETU-AI.
Expose les endpoints de chat SSE, de gestion des modèles Hugging Face, de persistance SQLite,
du handshake avec etu-aix.alwaysdata.net et sert l'interface web statique.
"""

import json
from pathlib import Path
from typing import Any, Optional
import urllib.request

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import sys
BASE_DIR = Path(__file__).resolve().parent
AI_SERVER_DIR = BASE_DIR / "ai-server"
if str(AI_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(AI_SERVER_DIR))

from chat.sse_handler import ChatStreamHandler
from configs import init_file_logger
from install import scan_hardware

init_file_logger("api_server.log")
from models_mgr.catalog import get_catalog_with_status
from models_mgr.downloader import ModelDownloader
from models_mgr.hf_client import hf_client
from storage.db import StorageManager
from storage.state_manager import state_manager
from tools.executor import ToolExecutor

from setup_wizard import wizard_manager

UI_DIR = BASE_DIR / "ui"

storage = StorageManager()
tool_executor = ToolExecutor()
downloader = ModelDownloader(storage=storage)
stream_handler = ChatStreamHandler(storage=storage, tool_executor=tool_executor)

app = FastAPI(title="ETU-AI Core Server", version="1.0.0")

@app.on_event("startup")
def on_startup():
    """Initialisation au démarrage du serveur : enregistre l'état et lance Beacon si setup déjà fait."""
    import threading
    state_manager.record_startup()
    try:
        hw = scan_hardware()
        state_manager.update_state({
            "hardware": {
                "cpu": hw.get("cpu", {}).get("processor"),
                "cpu_cores": hw.get("cpu", {}).get("count"),
                "gpus": hw.get("gpus", []),
                "npus": hw.get("npus", []),
                "has_npu": bool(hw.get("npus")),
                "recommended_backend": hw.get("recommended_backend"),
            }
        })
    except Exception as e:
        print(f"[!] Erreur scan hardware au démarrage: {e}")

    # Si le setup a déjà été complété, lancer MAIA-Beacon en arrière-plan sans bloquer
    if wizard_manager.is_completed():
        threading.Thread(target=wizard_manager.ensure_beacon_running, daemon=True).start()

# Configuration CORS pour etu-aix.alwaysdata.net, port 8900 et localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://etu-aix.alwaysdata.net",
        "http://etu-aix.alwaysdata.net",
        "http://localhost:8900",
        "http://127.0.0.1:8900",
        "http://localhost:11343",
        "http://127.0.0.1:11343",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------------------------------------------------------
# Modèles Pydantic pour validation
# -----------------------------------------------------------------------------
class SetupStartRequest(BaseModel):
    selected_models: list[dict[str, Any]]
# -----------------------------------------------------------------------------
class ChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    message: str
    model: Optional[str] = None


class DownloadRequest(BaseModel):
    repo: str
    filename: str


class ModelSelectRequest(BaseModel):
    model: str
    context_size: Optional[int] = 4096


class HandshakeRequest(BaseModel):
    pairing_token: str
    student_id: Optional[str] = None


# -----------------------------------------------------------------------------
# Endpoints de Chat & Streaming
# -----------------------------------------------------------------------------
@app.post("/api/chat/stream")
async def chat_stream_endpoint(req: ChatRequest):
    """Point d'entrée du streaming SSE pour les réponses de l'IA."""
    conv_id = req.conversation_id
    if not conv_id:
        conv_id = storage.create_conversation(title=req.message[:30] + "...")

    return StreamingResponse(
        stream_handler.stream_chat(conv_id, req.message, req.model),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Conversation-Id": conv_id,
        },
    )


# -----------------------------------------------------------------------------
# Endpoints de Conversations (SQLite)
# -----------------------------------------------------------------------------
@app.get("/api/conversations")
def list_conversations():
    return {"conversations": storage.list_conversations()}


@app.post("/api/conversations")
def create_conversation(data: dict[str, Any]):
    title = data.get("title", "Nouvelle discussion")
    conv_id = storage.create_conversation(title=title)
    return {"conversation_id": conv_id, "title": title}


@app.get("/api/conversations/{conv_id}/messages")
def get_messages(conv_id: str):
    conv = storage.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    return {"conversation": conv, "messages": storage.get_messages(conv_id)}


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str):
    success = storage.delete_conversation(conv_id)
    return {"success": success}


# -----------------------------------------------------------------------------
# Endpoints de Gestion des Modèles
# -----------------------------------------------------------------------------
@app.get("/api/models/catalog")
def get_catalog():
    hw = scan_hardware()
    installed = set(downloader.list_installed_models())
    # 1. Base catalogue de référence et modèles locaux
    base_catalog = get_catalog_with_status(hw, list(installed))
    seen_filenames = {m.get("filename") for m in base_catalog if m.get("filename")}
    seen_repos = {m.get("repo", "").lower() for m in base_catalog if m.get("repo")}

    # 2. Enrichissement automatique avec toutes les IA compatibles Hugging Face
    hf_compatible = hf_client.get_all_compatible_models(
        hardware_info=hw,
        query="",
        limit=60,
        only_compatible=True,
    )
    for hf_m in hf_compatible:
        fn = hf_m.get("filename")
        repo = hf_m.get("repo", "").lower()
        if fn not in seen_filenames and repo not in seen_repos:
            if fn in installed:
                hf_m["is_installed"] = True
            base_catalog.append(hf_m)
            seen_filenames.add(fn)
            seen_repos.add(repo)

    return {
        "catalog": base_catalog,
        "installed": list(installed),
        "hardware": {
            "recommended_backend": hw.get("recommended_backend"),
            "gpus": hw.get("gpus", []),
            "npus": hw.get("npus", []),
            "has_npu": bool(hw.get("npus")),
            "cpu_cores": hw.get("cpu", {}).get("count"),
            "cpu_processor": hw.get("cpu", {}).get("processor"),
            "ram_total_gib": hw.get("ram_total_gib"),
            "ram_available_gib": hw.get("ram_available_gib"),
        },
    }


@app.get("/api/models/huggingface/search")
def search_huggingface_models(
    q: str = "",
    sort: str = "downloads",
    limit: int = 25,
    only_compatible: bool = True,
):
    """Recherche des modèles GGUF sur Hugging Face sans credential, avec évaluation de compatibilité matérielle."""
    hw = scan_hardware()
    installed = set(downloader.list_installed_models())
    results = hf_client.get_all_compatible_models(
        hardware_info=hw,
        query=q,
        limit=limit,
        only_compatible=only_compatible,
    )
    for m in results:
        fn = m.get("filename")
        if fn and fn in installed:
            m["is_installed"] = True
    return {
        "query": q,
        "total": len(results),
        "models": results,
        "hardware": {
            "ram_total_gib": hw.get("ram_total_gib"),
            "ram_available_gib": hw.get("ram_available_gib"),
            "recommended_backend": hw.get("recommended_backend"),
            "has_npu": bool(hw.get("npus")),
            "npus": hw.get("npus", []),
            "gpus": hw.get("gpus", []),
        },
    }


@app.get("/api/models/huggingface/compatible")
def get_all_compatible_hf_models(limit: int = 30):
    """Retourne toutes les IA Hugging Face possibles et compatibles avec la configuration matérielle du PC."""
    hw = scan_hardware()
    installed = set(downloader.list_installed_models())
    models = hf_client.get_all_compatible_models(
        hardware_info=hw,
        query="",
        limit=limit,
        only_compatible=True,
    )
    for m in models:
        fn = m.get("filename")
        if fn and fn in installed:
            m["is_installed"] = True
    return {
        "total": len(models),
        "models": models,
        "hardware": {
            "ram_total_gib": hw.get("ram_total_gib"),
            "ram_available_gib": hw.get("ram_available_gib"),
            "recommended_backend": hw.get("recommended_backend"),
            "has_npu": bool(hw.get("npus")),
            "npus": hw.get("npus", []),
            "gpus": hw.get("gpus", []),
        },
    }


@app.get("/api/models/huggingface/files")
def get_hf_model_files(repo: str):
    """Liste tous les fichiers GGUF d'un dépôt Hugging Face avec calcul de compatibilité PC pour chaque fichier."""
    hw = scan_hardware()
    files = hf_client.get_model_files(repo)
    for f in files:
        f["compatibility"] = hf_client.evaluate_model_compatibility(
            size_gib=f.get("size_gib", 2.0),
            model_name=f.get("filename", repo),
            hardware_info=hw,
            quant=f.get("quant", "Q4_K_M"),
        )
    return {"repo": repo, "files": files}


@app.get("/api/models/auto-select")
def auto_select_best_models(count: int = 1):
    """Choisit automatiquement les IA les plus adaptées et optimales pour la configuration matérielle du PC."""
    hw = scan_hardware()
    installed = set(downloader.list_installed_models())
    best = hf_client.auto_select_best_models(hardware_info=hw, top_k=count)
    for m in best:
        fn = m.get("filename")
        if fn and fn in installed:
            m["is_installed"] = True
    return {
        "recommended_models": best,
        "primary_model": best[0] if best else None,
        "hardware": {
            "ram_total_gib": hw.get("ram_total_gib"),
            "ram_available_gib": hw.get("ram_available_gib"),
            "recommended_backend": hw.get("recommended_backend"),
            "has_npu": bool(hw.get("npus")),
            "npus": hw.get("npus", []),
            "gpus": hw.get("gpus", []),
            "cpu_cores": hw.get("cpu", {}).get("count"),
            "cpu_processor": hw.get("cpu", {}).get("processor"),
        },
    }


@app.post("/api/models/auto-select/apply")
def apply_auto_selected_model():
    """Sélectionne ou initialise automatiquement la meilleure IA pour ce PC."""
    hw = scan_hardware()
    best = hf_client.auto_select_best_models(hardware_info=hw, top_k=1)
    if not best:
        raise HTTPException(status_code=404, detail="Aucun modèle compatible détecté")
    target = best[0]
    filename = target.get("filename")
    clean_stem = filename[:-5] if filename and filename.lower().endswith(".gguf") else (filename or "model")
    storage.set_setting("selected_model", clean_stem)
    return {
        "success": True,
        "selected_model": target,
        "active_model": clean_stem,
    }


@app.post("/api/models/download")
def download_model(req: DownloadRequest):
    task_id = downloader.start_download(req.repo, req.filename)
    return {"task_id": task_id, "status": "started"}


@app.get("/api/models/tasks/{task_id}")
def get_download_task(task_id: str):
    task = downloader.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tâche introuvable")
    return task


@app.post("/api/models/select")
def select_model(req: ModelSelectRequest):
    """Bascule à chaud du modèle via l'API de MAIA-Beacon."""
    clean_model = req.model[:-5] if req.model.lower().endswith(".gguf") else req.model
    storage.set_setting("selected_model", clean_model)
    try:
        beacon_url = "http://127.0.0.1:11343/api/select"
        payload = json.dumps({"model": clean_model, "context_size": req.context_size}).encode("utf-8")
        req_post = urllib.request.Request(
            beacon_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_post, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"success": True, "active_model": clean_model, "result": data}
    except Exception as e:
        return {
            "success": True,
            "active_model": clean_model,
            "beacon_online": False,
            "message": f"Modèle '{clean_model}' sélectionné ({e}).",
        }


# -----------------------------------------------------------------------------
# Setup Wizard Endpoints
# -----------------------------------------------------------------------------
@app.get("/api/setup/status")
def get_setup_status():
    return wizard_manager.get_status()


@app.get("/api/setup/state")
def get_app_state():
    """Retourne l'état complet de l'application (ce qui est installé, config, etc.)."""
    return state_manager.get_state()


@app.post("/api/setup/state")
def update_app_state(updates: dict[str, Any]):
    """Permet de mettre à jour des clés de l'état persistant."""
    return state_manager.update_state(updates)


@app.post("/api/setup/start")
def start_setup_endpoint(req: SetupStartRequest):
    started = wizard_manager.start_setup(req.selected_models)
    return {"started": started, "status": wizard_manager.status}


@app.post("/api/setup/reset")
def reset_setup_endpoint():
    storage.set_setting("setup_completed", "false")
    state_manager.reset_setup()
    wizard_manager.status = "idle"
    return {"success": True}


# -----------------------------------------------------------------------------
# Handshake & Sécurité etu-aix
# -----------------------------------------------------------------------------
@app.post("/api/handshake")
def process_handshake(req: HandshakeRequest):
    """Enregistre le token d'appairage étudiant pour autoriser etu-aix.alwaysdata.net."""
    storage.set_setting("pairing_token", req.pairing_token)
    if req.student_id:
        storage.set_setting("student_id", req.student_id)
        tool_executor.auth_token = req.pairing_token
    return {
        "success": True,
        "message": "Appairage avec etu-aix.alwaysdata.net validé.",
        "student_id": req.student_id,
    }


@app.get("/api/status")
def server_status():
    hw = scan_hardware()
    installed = downloader.list_installed_models()
    token = storage.get_setting("pairing_token")
    beacon_online = wizard_manager.is_beacon_online()

    active_model = None
    active_engine = None
    if beacon_online:
        try:
            req_b = urllib.request.Request("http://127.0.0.1:11343/api/status")
            with urllib.request.urlopen(req_b, timeout=2) as resp:
                bdata = json.loads(resp.read().decode("utf-8"))
                active_model = bdata.get("active_model")
                active_engine = bdata.get("active_engine")
        except Exception:
            pass

    saved_selected_model = storage.get_setting("selected_model")
    if not active_model and saved_selected_model:
        active_model = saved_selected_model
    elif not active_model and installed:
        active_model = installed[0].replace(".gguf", "")

    catalog_items = get_catalog_with_status(hw, installed)
    installed_catalog = [m for m in catalog_items if m.get("is_installed")]

    return {
        "status": "online",
        "setup_completed": wizard_manager.is_completed(),
        "beacon_online": beacon_online,
        "active_model": active_model,
        "active_engine": active_engine,
        "paired_with_etu": bool(token),
        "installed_models_count": len(installed),
        "installed_models": installed_catalog,
        "backend": hw.get("recommended_backend"),
        "gpus": hw.get("gpus", []),
        "npus": hw.get("npus", []),
        "has_npu": bool(hw.get("npus")),
        "cpu_cores": hw.get("cpu", {}).get("count"),
        "cpu_processor": hw.get("cpu", {}).get("processor"),
        "ram_total_gib": hw.get("ram_total_gib"),
        "ram_available_gib": hw.get("ram_available_gib"),
    }


# -----------------------------------------------------------------------------
# Interface Web Statique (UI)
# -----------------------------------------------------------------------------
if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")

@app.get("/")
def serve_index():
    index_path = UI_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return JSONResponse({"status": "ETU-AI API active", "ui": "loading"})
