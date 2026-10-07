"""
Serveur API FastAPI principal pour ETU-AI.
Expose les endpoints de chat SSE, de gestion des modèles Hugging Face, de persistance SQLite,
du handshake avec etu-aix.alwaysdata.net et sert l'interface web statique.
"""

import json
import asyncio
import hashlib
import struct
import base64
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional
from uuid import UUID, uuid4
import urllib.request
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import sys
BASE_DIR = Path(__file__).resolve().parent
AI_SERVER_DIR = BASE_DIR / "ai-server"
if str(AI_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(AI_SERVER_DIR))

from chat.sse_handler import ChatStreamHandler
from chat.attachments import prepare_attachments, load_attachments
from configs import init_file_logger
from install import scan_hardware

init_file_logger("api_server.log")
from models_mgr.catalog import get_catalog_with_status
from models_mgr.downloader import ModelDownloader
from models_mgr.projectors import ProjectorManager, is_projector, known_text_only
from models_mgr.hf_client import hf_client
from storage.db import StorageManager
from storage.api_credentials import delete_api_key, get_api_key, set_api_key
from storage.state_manager import state_manager
from tools.executor import ToolExecutor
from integration.api import router as etu_router, LOCAL_ORIGINS, admin as require_local_admin
from integration.bridge import bridge, ETU_ORIGINS, ALLOW_LOCALHOST_ORIGINS, LOCALHOST_ORIGIN_REGEX, is_etu_origin

from setup_wizard import wizard_manager

logger = logging.getLogger(__name__)

UI_DIR = BASE_DIR / "ui"

storage = StorageManager()
tool_executor = ToolExecutor()
downloader = ModelDownloader(storage=storage)
stream_handler = ChatStreamHandler(storage=storage, tool_executor=tool_executor)
_active_chat_conversations: set[str] = set()

app = FastAPI(title="ETU-AI Core Server", version="1.0.0")
app.state.etu_storage = storage

@app.on_event("startup")
def on_startup():
    """Initialisation au démarrage du serveur : enregistre l'état et lance Beacon si setup déjà fait."""
    import threading
    bridge.identity()
    try:
        state_manager.record_startup()
    except OSError as exc:
        print(f"[!] Horodatage du démarrage non enregistré : {exc}")
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

# Les origines ETU accèdent uniquement au bridge /v1, jamais à l'administration.
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ETU_ORIGINS | LOCAL_ORIGINS),
    allow_origin_regex=LOCALHOST_ORIGIN_REGEX if ALLOW_LOCALHOST_ORIGINS else None,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def local_boundary(request: Request, call_next):
    host = request.url.hostname
    test_client = request.client and request.client.host == "testclient"
    if host not in {"127.0.0.1", "localhost", "::1"} and not (test_client and host == "testserver"):
        return JSONResponse({"detail": "Hôte local requis"}, status_code=400)
    origin = request.headers.get("origin")
    if origin and not (is_etu_origin(origin) or origin in LOCAL_ORIGINS):
        return JSONResponse({"detail": "Origine non autorisée"}, status_code=403)
    if is_etu_origin(origin) and origin not in LOCAL_ORIGINS and not request.url.path.startswith("/v1/"):
        return JSONResponse({"detail": "ETU accède uniquement au bridge en lecture seule"}, status_code=403)
    if request.url.path.startswith("/v1/") and request.method not in {"GET", "OPTIONS"}:
        size = 0
        chunks = []
        limit = 24 * 1024 * 1024 + 131072 if request.method == "PUT" and request.url.path == "/v1/context" else 262144
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                return JSONResponse({"detail": {"code": "workspace_images_invalid"} if limit > 262144 else "Requête limitée à 256 Kio"}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
    response = await call_next(request)
    if request.url.path.startswith(("/v1/", "/api/etu/")):
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(etu_router)


@app.get("/api/launch-mode")
def get_launch_mode():
    mode = os.environ.get("ETU_LAUNCH_MODE", "normal").strip().lower()
    if mode not in {"normal", "local", "server"}:
        mode = "normal"
    return {
        "mode": mode,
        "beacon_host": "0.0.0.0" if mode == "server" else "127.0.0.1",
        "beacon_port": 11343,
        "localhost_origins_allowed": ALLOW_LOCALHOST_ORIGINS,
    }


@app.get("/api/etu/remote-tools")
def get_remote_tool_consent(request: Request):
    require_local_admin(request)
    return {"enabled": bool(storage.get_setting("etu_remote_tools_enabled", False))}


@app.put("/api/etu/remote-tools")
def set_remote_tool_consent(request: Request, body: dict[str, Any]):
    require_local_admin(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool) or set(body) != {"enabled"}:
        raise HTTPException(status_code=422, detail="Valeur de consentement invalide.")
    storage.set_setting("etu_remote_tools_enabled", enabled)
    return {"enabled": enabled}


@app.on_event("startup")
async def start_bridge_cleanup():
    async def cleanup():
        while True:
            await asyncio.sleep(15)
            with bridge.lock:
                bridge.prune()
    app.state.bridge_cleanup = asyncio.create_task(cleanup())


@app.on_event("shutdown")
async def stop_bridge_cleanup():
    task = getattr(app.state, "bridge_cleanup", None)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# -----------------------------------------------------------------------------
# Modèles Pydantic pour validation
# -----------------------------------------------------------------------------
class SetupStartRequest(BaseModel):
    selected_models: list[dict[str, Any]]
# -----------------------------------------------------------------------------
class AttachmentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    data: str = Field(max_length=13981016)


class ChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    message: str = ""
    model: Optional[str] = None
    profile_id: Optional[str] = None
    attachments: list[AttachmentRequest] = Field(default_factory=list, max_length=6)
    request_id: Optional[UUID] = None


class ProfileRequest(BaseModel):
    name: str
    avatar: str = "✦"
    instructions: str = ""
    model: Optional[str] = None


class ProviderSettingsRequest(BaseModel):
    provider: str = "openai-compatible"
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    models: list[str] = Field(default_factory=list)
    api_key: Optional[str] = None
    clear_api_key: bool = False


class ProviderModelsRequest(BaseModel):
    provider: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None


PROVIDER_PRESETS = {
    "openai-compatible": {"label": "Autre API compatible OpenAI", "type": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "openai": {"label": "OpenAI", "type": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "openrouter": {"label": "OpenRouter", "type": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-4o-mini"},
    "groq": {"label": "Groq", "type": "openai", "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
    "mistral": {"label": "Mistral", "type": "openai", "base_url": "https://api.mistral.ai/v1", "model": "mistral-small-latest"},
    "together": {"label": "Together AI", "type": "openai", "base_url": "https://api.together.xyz/v1", "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo"},
    "deepseek": {"label": "DeepSeek", "type": "openai", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "xai": {"label": "xAI", "type": "openai", "base_url": "https://api.x.ai/v1", "model": "grok-3-mini"},
    "cerebras": {"label": "Cerebras", "type": "openai", "base_url": "https://api.cerebras.ai/v1", "model": "llama-3.3-70b"},
    "fireworks": {"label": "Fireworks AI", "type": "openai", "base_url": "https://api.fireworks.ai/inference/v1", "model": "accounts/fireworks/models/llama-v3p3-70b-instruct"},
    "perplexity": {"label": "Perplexity", "type": "openai", "base_url": "https://api.perplexity.ai", "model": "sonar"},
    "nvidia": {"label": "NVIDIA NIM", "type": "openai", "base_url": "https://integrate.api.nvidia.com/v1", "model": "meta/llama-3.3-70b-instruct"},
    "sambanova": {"label": "SambaNova", "type": "openai", "base_url": "https://api.sambanova.ai/v1", "model": "Meta-Llama-3.3-70B-Instruct"},
    "deepinfra": {"label": "DeepInfra", "type": "openai", "base_url": "https://api.deepinfra.com/v1/openai", "model": "meta-llama/Meta-Llama-3.1-70B-Instruct"},
    "github-models": {"label": "GitHub Models", "type": "openai", "base_url": "https://models.inference.ai.azure.com", "model": "gpt-4o-mini"},
    "anthropic": {"label": "Anthropic", "type": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-haiku-4-5-20251001"},
    "gemini": {"label": "Google Gemini", "type": "gemini", "base_url": "https://generativelanguage.googleapis.com", "model": "gemini-3.8-flash"},
    "custom": {"label": "Autre API compatible OpenAI", "type": "openai", "base_url": "https://api.openai.com/v1", "model": ""},
}


def validate_profile(req: ProfileRequest) -> tuple[str, str, str]:
    name = req.name.strip()
    avatar = req.avatar.strip() or "✦"
    instructions = req.instructions.strip()
    image_prefix = "data:image/webp;base64,"
    if avatar.startswith(image_prefix):
        encoded = avatar[len(image_prefix):]
        if len(encoded) > 95000 or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", encoded):
            raise HTTPException(status_code=422, detail="Image de profil invalide")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise HTTPException(status_code=422, detail="Image de profil invalide")
        if len(image_bytes) > 70000 or image_bytes[:4] != b"RIFF" or image_bytes[8:12] != b"WEBP":
            raise HTTPException(status_code=422, detail="Image de profil invalide")
    elif len(avatar) > 20:
        raise HTTPException(status_code=422, detail="Tête de profil trop longue")
    if not name or len(name) > 50 or len(instructions) > 3000:
        raise HTTPException(status_code=422, detail="Nom ou instructions invalides")
    return name, avatar, instructions


class DownloadRequest(BaseModel):
    repo: str
    filename: str


class ModelSelectRequest(BaseModel):
    model: str
    context_size: Optional[int] = None
    source: str = "local"
    provider: Optional[str] = None


class ProjectorRequest(BaseModel):
    model: str
    repo: str
    filename: str


class HandshakeRequest(BaseModel):
    pairing_token: str
    student_id: Optional[str] = None


# -----------------------------------------------------------------------------
# Endpoints de Chat & Streaming
# -----------------------------------------------------------------------------
@app.post("/api/chat/stream")
async def chat_stream_endpoint(req: ChatRequest):
    """Point d'entrée du streaming SSE pour les réponses de l'IA."""
    if not req.message.strip() and not req.attachments:
        raise HTTPException(status_code=422, detail="Ajoutez un message ou un fichier.")
    try:
        attachments = await asyncio.to_thread(prepare_attachments, req.attachments)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    conv_id = req.conversation_id
    if conv_id:
        conversation = storage.get_conversation(conv_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation introuvable")
        profile_id = conversation.get("profile_id") or "default"
    else:
        profile_id = req.profile_id or storage.get_setting("active_profile", "default")
        if not storage.get_profile(profile_id):
            raise HTTPException(status_code=404, detail="Profil introuvable")
        title = req.message.strip() or attachments[0]["name"]
        conv_id = storage.create_conversation(title=title[:30] + "...", profile_id=profile_id)
    profile = storage.get_profile(profile_id) or storage.get_profile("default")

    request_id = str(req.request_id or uuid4())
    if request_id in stream_handler.active_requests:
        raise HTTPException(status_code=409, detail="Cette génération est déjà active")
    if conv_id in _active_chat_conversations:
        raise HTTPException(status_code=409, detail="Une réponse est déjà en cours dans cette discussion.")
    _active_chat_conversations.add(conv_id)

    async def guarded_stream():
        stream = stream_handler.stream_chat(conv_id, req.message, req.model or profile.get("model"), profile, attachments=attachments, request_id=request_id)
        try:
            async for event in stream:
                yield event
        finally:
            try:
                await stream.aclose()
            finally:
                _active_chat_conversations.discard(conv_id)

    return StreamingResponse(
        guarded_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Conversation-Id": conv_id,
            "X-Request-Id": request_id,
            "X-Attachment-Warnings": json.dumps([item["warning"].strip() for item in attachments if item["warning"]]),
        },
    )


# -----------------------------------------------------------------------------
# Endpoints de Conversations (SQLite)
# -----------------------------------------------------------------------------
@app.post("/api/chat/requests/{request_id}/stop")
async def stop_chat(request_id: UUID):
    return {"cancelled": stream_handler.cancel(str(request_id))}


@app.get("/api/conversations")
def list_conversations():
    return {"conversations": storage.list_conversations()}


@app.post("/api/conversations")
def create_conversation(data: dict[str, Any]):
    title = data.get("title", "Nouvelle discussion")
    profile_id = data.get("profile_id") or storage.get_setting("active_profile", "default")
    if not storage.get_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profil introuvable")
    conv_id = storage.create_conversation(title=title, profile_id=profile_id)
    return {"conversation_id": conv_id, "title": title}


@app.get("/api/conversations/{conv_id}/messages")
def get_messages(conv_id: str):
    conv = storage.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    messages = load_attachments(storage, storage.get_messages(conv_id))
    for message in messages:
        for attachment in message["attachments"]:
            attachment.pop("text", None)
    return {"conversation": conv, "messages": messages}


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str):
    success = storage.delete_conversation(conv_id)
    return {"success": success}


@app.get("/api/profiles")
def list_profiles():
    return {"profiles": storage.list_profiles(), "active_profile_id": storage.get_setting("active_profile", "default")}


@app.post("/api/profiles")
def create_profile(req: ProfileRequest):
    name, avatar, instructions = validate_profile(req)
    return storage.save_profile(name, avatar, instructions, req.model)


@app.put("/api/profiles/{profile_id}")
def update_profile(profile_id: str, req: ProfileRequest):
    if not storage.get_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profil introuvable")
    name, avatar, instructions = validate_profile(req)
    return storage.save_profile(name, avatar, instructions, req.model, profile_id)


@app.post("/api/profiles/{profile_id}/activate")
def activate_profile(profile_id: str):
    if not storage.get_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profil introuvable")
    storage.set_setting("active_profile", profile_id)
    return {"active_profile_id": profile_id}


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: str):
    if profile_id == "default":
        raise HTTPException(status_code=400, detail="Le profil initial ne peut pas être supprimé")
    if not storage.delete_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profil introuvable")
    if storage.get_setting("active_profile", "default") == profile_id:
        storage.set_setting("active_profile", "default")
    return {"success": True}


# -----------------------------------------------------------------------------
# Endpoints de Gestion des Modèles
# -----------------------------------------------------------------------------
def get_configured_api_models() -> list[dict[str, Any]]:
    state = storage.get_setting("api_providers", {}) or {}
    configs = state.get("configs", {})
    legacy = storage.get_setting("api_provider", {}) or {}
    if not configs and legacy:
        configs = {legacy.get("provider", "openai-compatible"): legacy}
    models = []
    for provider_id, config in configs.items():
        selected_models = config.get("models") or [config.get("model", "")]
        selected_models = [str(name).strip() for name in selected_models if str(name).strip()]
        preferred_model = str(config.get("model", "")).strip()
        if preferred_model in selected_models:
            selected_models.remove(preferred_model)
            selected_models.insert(0, preferred_model)
        if not selected_models:
            continue
        try:
            has_key = bool(get_api_key(provider_id))
        except Exception:
            has_key = False
        if not has_key:
            continue
        preset = PROVIDER_PRESETS.get(provider_id, {})
        for model_name in selected_models:
            models.append({
                "id": f"api:{provider_id}:{model_name}",
                "model": model_name,
                "name": model_name,
                "provider": provider_id,
                "provider_label": preset.get("label", provider_id),
                "source": "api",
            })
    return models


@app.get("/api/models/catalog")
def get_catalog():
    hw = scan_hardware()
    installed = set(downloader.list_installed_models())
    # Le catalogue local doit rester disponible même si Hugging Face est lent.
    # L'exploration distante possède son propre onglet et son propre endpoint.
    base_catalog = get_catalog_with_status(hw, list(installed))
    projectors = ProjectorManager(storage, downloader, hf_client)
    for item in base_catalog:
        item["mmproj"] = projectors.describe(item["filename"])

    return {
        "catalog": base_catalog,
        "installed": list(installed),
        "remote_models": get_configured_api_models(),
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
    if filename not in downloader.list_installed_models():
        raise HTTPException(status_code=409, detail="Le modèle recommandé doit d'abord être installé")
    result = select_model(ModelSelectRequest(model=filename))
    return {**result, "selected_model": target}


@app.post("/api/models/download")
def download_model(req: DownloadRequest):
    try:
        downloader.validate_reference(req.repo, req.filename)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if is_projector(req.filename):
        raise HTTPException(status_code=422, detail="Ajoutez ce projecteur depuis la gestion Vision / mmproj du modèle concerné.")
    if req.filename in downloader.list_installed_models():
        raise HTTPException(status_code=409, detail="Ce fichier GGUF est déjà installé")
    files = hf_client.get_model_files(req.repo)
    if not files:
        raise HTTPException(status_code=503, detail="Impossible de vérifier les fichiers du dépôt Hugging Face")
    selected_file = next((entry for entry in files if entry["filename"] == req.filename), None)
    if not selected_file:
        raise HTTPException(status_code=404, detail="Fichier GGUF absent du dépôt")
    try:
        task_id = downloader.start_download(req.repo, req.filename, selected_file.get("sha256"))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"task_id": task_id, "status": "started"}


@app.get("/api/models/tasks/{task_id}")
def get_download_task(task_id: str):
    task = downloader.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tâche introuvable")
    return task


@app.post("/api/models/tasks/{task_id}/cancel")
def cancel_download_task(task_id: str):
    if not downloader.cancel_download(task_id):
        raise HTTPException(status_code=404, detail="Tâche active introuvable")
    return {"cancelled": True}


@app.get("/api/models/mmproj")
def get_model_projectors(model: str, repo: Optional[str] = None):
    try:
        downloader.validate_reference(repo or "local/models", model)
        return ProjectorManager(storage, downloader, hf_client).discover(model, repo)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/models/mmproj/download")
def install_model_projector(req: ProjectorRequest):
    try:
        downloader.validate_reference(req.repo, req.filename)
        return ProjectorManager(storage, downloader, hf_client).install(req.model, req.repo, req.filename)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.delete("/api/models/mmproj")
def remove_model_projector(model: str):
    try:
        ProjectorManager(storage, downloader, hf_client).assign(model, None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"success": True}


@app.post("/api/models/mmproj/import")
async def import_model_projector(request: Request, model: str, filename: str):
    """Stream a local GGUF without buffering gigabytes in the web client or API."""
    try:
        downloader.validate_reference("local/projectors", filename)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if model not in downloader.list_installed_models() or known_text_only(model) or not is_projector(filename):
        raise HTTPException(status_code=422, detail="Choisissez un modèle vision installé et un fichier mmproj/projector GGUF.")
    temporary = downloader.models_dir / f"mmproj-import-{uuid4().hex}.part"
    limit = 8 * 1024 ** 3
    size = 0
    digest = hashlib.sha256()
    header = bytearray()
    try:
        with temporary.open("wb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail="8 Go maximum par projecteur.")
                header.extend(chunk[:max(0, 24 - len(header))])
                digest.update(chunk)
                await asyncio.to_thread(output.write, chunk)
        if len(header) < 24 or header[:4] != b"GGUF" or struct.unpack_from("<I", header, 4)[0] not in {2, 3}:
            raise HTTPException(status_code=422, detail="Projecteur GGUF invalide.")
        local = f"mmproj-local-{digest.hexdigest()[:20]}.gguf"
        destination = downloader.models_dir / local
        if not destination.exists():
            temporary.replace(destination)
        manager = ProjectorManager(storage, downloader, hf_client)
        manager.assign(model, {"filename": filename, "local_filename": local, "repo": "local", "manual": True})
        return {"success": True, **manager.describe(model)}
    finally:
        temporary.unlink(missing_ok=True)


@app.post("/api/models/select")
def select_model(req: ModelSelectRequest):
    """Bascule à chaud du modèle via l'API de MAIA-Beacon."""
    if req.source == "api":
        if not req.provider or req.provider not in PROVIDER_PRESETS:
            raise HTTPException(status_code=400, detail="Fournisseur API invalide")
        state = storage.get_setting("api_providers", {}) or {}
        configs = state.get("configs", {})
        legacy = storage.get_setting("api_provider", {}) or {}
        if not configs and legacy:
            configs = {legacy.get("provider", "openai-compatible"): legacy}
        config = configs.get(req.provider)
        allowed_models = config.get("models") or [config.get("model")]
        if not config or req.model not in allowed_models:
            raise HTTPException(status_code=404, detail="Modèle API non configuré")
        try:
            if not get_api_key(req.provider):
                raise HTTPException(status_code=400, detail="Clé API manquante pour ce fournisseur")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Coffre de clés indisponible") from exc
        config["model"] = req.model
        storage.set_setting("api_providers", {"active_provider": req.provider, "configs": configs})
        return {"success": True, "active_model": req.model, "active_model_id": f"api:{req.provider}:{req.model}", "active_provider": req.provider}
    if req.source != "local":
        raise HTTPException(status_code=400, detail="Source de modèle invalide")
    if _active_chat_conversations:
        raise HTTPException(status_code=409, detail="Arrêtez la génération avant de changer de modèle ou de projecteur.")
    clean_model = req.model[:-5] if req.model.lower().endswith(".gguf") else req.model
    if f"{clean_model}.gguf" not in downloader.list_installed_models():
        raise HTTPException(status_code=404, detail="Modèle non installé")
    try:
        mmproj = ProjectorManager(storage, downloader, hf_client).selected(f"{clean_model}.gguf")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        beacon_url = "http://127.0.0.1:11343/api/select"
        selection: dict[str, Any] = {"model": clean_model}
        # A non-existent explicit path disables Beacon's broad family-based auto-detection.
        # Exact projector associations are resolved and validated by this application.
        selection["mmproj"] = mmproj or "__etu_no_mmproj__.gguf"
        with urllib.request.urlopen("http://127.0.0.1:11343/api/status", timeout=3) as status_resp:
            current = json.loads(status_resp.read().decode("utf-8"))
        if current.get("active_model") == clean_model and current.get("status") == "running" and current.get("active_mmproj") != mmproj:
            stop_request = urllib.request.Request("http://127.0.0.1:11343/api/stop", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(stop_request, timeout=10):
                pass
        if req.context_size is not None:
            selection["context_size"] = req.context_size
        payload = json.dumps(selection).encode("utf-8")
        req_post = urllib.request.Request(
            beacon_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_post, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("success") is False:
                raise HTTPException(status_code=502, detail=data.get("error", "Beacon a refusé le modèle"))
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            with urllib.request.urlopen("http://127.0.0.1:11343/api/status", timeout=3) as status_resp:
                status = json.loads(status_resp.read().decode("utf-8"))
            if status.get("status") == "error":
                raise HTTPException(status_code=502, detail=status.get("error_message") or "Chargement du modèle échoué")
            if status.get("status") == "running" and status.get("active_model") == clean_model and status.get("active_mmproj") != mmproj:
                raise HTTPException(status_code=502, detail="Le moteur n'a pas chargé le projecteur associé. Vérifiez sa compatibilité avec cette variante du modèle.")
            if status.get("status") == "running" and status.get("active_model") == clean_model and status.get("active_mmproj") == mmproj:
                storage.set_setting("selected_model", clean_model)
                provider_state = storage.get_setting("api_providers", {}) or {}
                if provider_state:
                    provider_state["active_provider"] = None
                    storage.set_setting("api_providers", provider_state)
                legacy_provider = storage.get_setting("api_provider", {}) or {}
                if legacy_provider.get("enabled"):
                    legacy_provider["enabled"] = False
                    storage.set_setting("api_provider", legacy_provider)
                return {"success": True, "active_model": clean_model, "active_mmproj": mmproj, "result": status}
            time.sleep(0.5)
        raise HTTPException(status_code=504, detail="Délai de chargement du modèle dépassé")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Beacon indisponible : {e}")


# -----------------------------------------------------------------------------
# Setup Wizard Endpoints
# -----------------------------------------------------------------------------
@app.get("/api/setup/status")
def get_setup_status():
    return wizard_manager.get_status()


@app.get("/api/settings/provider")
def get_provider_settings(provider: Optional[str] = None):
    state = storage.get_setting("api_providers", {}) or {}
    configs = state.get("configs", {})
    legacy = storage.get_setting("api_provider", {}) or {}
    if not configs and legacy:
        legacy_id = legacy.get("provider", "openai-compatible")
        configs[legacy_id] = legacy
    provider = provider or state.get("active_provider") or legacy.get("provider") or "openai-compatible"
    if provider not in PROVIDER_PRESETS:
        raise HTTPException(status_code=400, detail="Fournisseur inconnu.")
    config = configs.get(provider, {})
    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["custom"])
    try:
        has_key = bool(get_api_key(provider))
    except Exception:
        has_key = False
    configured = {}
    for key, value in configs.items():
        try:
            configured[key] = {"base_url": value.get("base_url"), "model": value.get("model"), "has_api_key": bool(get_api_key(key))}
        except Exception:
            configured[key] = {"base_url": value.get("base_url"), "model": value.get("model"), "has_api_key": False}
    return {
        **preset,
        **config,
        "provider": provider,
        "enabled": state.get("active_provider") == provider or (not state and bool(legacy.get("enabled")) and legacy.get("provider", "openai-compatible") == provider),
        "active_provider": state.get("active_provider") if "active_provider" in state else (legacy.get("provider", "openai-compatible") if legacy.get("enabled") else None),
        "providers": PROVIDER_PRESETS,
        "configured": configured,
        "has_api_key": has_key,
        "api_key": "",
    }


@app.post("/api/settings/provider/models")
def list_provider_models(req: ProviderModelsRequest, origin: Optional[str] = Header(default=None, alias="Origin")):
    if origin:
        try:
            parsed_origin = urlsplit(origin)
            local_origin = parsed_origin.scheme in {"http", "https"} and parsed_origin.hostname in {"127.0.0.1", "localhost", "::1"}
        except ValueError:
            local_origin = False
        if not local_origin:
            raise HTTPException(status_code=403, detail="La découverte des modèles est réservée à l’interface locale.")
    preset = PROVIDER_PRESETS.get(req.provider)
    if not preset:
        raise HTTPException(status_code=400, detail="Fournisseur inconnu.")
    state = storage.get_setting("api_providers", {}) or {}
    saved = (state.get("configs", {}) or {}).get(req.provider, {})
    base_url = (req.base_url or saved.get("base_url") or preset["base_url"]).strip().rstrip("/")
    try:
        parsed = urlsplit(base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        valid_url = (parsed.scheme == "https" and bool(parsed.hostname)) or local_http
    except ValueError:
        valid_url = False
        parsed = None
    if not valid_url or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(status_code=400, detail="L’URL du fournisseur doit utiliser HTTPS (sauf pour un serveur local).")
    api_key = (req.api_key or "").strip()
    if not api_key:
        try:
            api_key = get_api_key(req.provider) or ""
        except Exception:
            api_key = ""
    if not api_key:
        raise HTTPException(status_code=400, detail="Saisissez ou enregistrez la clé API avant de charger les modèles.")

    headers = {"Accept": "application/json", "User-Agent": "ETU-AI/1.0"}
    endpoint = ""
    if preset["type"] == "anthropic":
        endpoint = f"{base_url}/v1/models?limit=1000"
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    elif preset["type"] == "gemini":
        endpoint = f"{base_url}/v1beta/models?{urlencode({'pageSize': 1000})}"
        headers["x-goog-api-key"] = api_key
    else:
        endpoint = f"{base_url}/models"
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        request = urllib.request.Request(endpoint, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read(2_000_000).decode("utf-8"))
    except Exception as exc:
        status = getattr(exc, "code", None)
        if status in {401, 403}:
            raise HTTPException(status_code=502, detail="Le fournisseur a refusé la clé API. Vérifiez la clé et ses droits.") from exc
        if status:
            raise HTTPException(status_code=502, detail=f"Le fournisseur a répondu avec le statut {status} lors du chargement des modèles.") from exc
        raise HTTPException(status_code=502, detail="Impossible de joindre le fournisseur ou de lire sa réponse de modèles.") from exc
    raw_models = payload.get("data", []) if isinstance(payload, dict) else []
    if preset["type"] == "gemini":
        raw_models = [item for item in payload.get("models", []) if "generateContent" in item.get("supportedGenerationMethods", [])]
    models = []
    for item in raw_models:
        model_id = item.get("id") or item.get("name") if isinstance(item, dict) else None
        capabilities = item.get("capabilities") if isinstance(item, dict) else None
        if isinstance(capabilities, dict) and capabilities.get("completion_chat") is False:
            continue
        if isinstance(model_id, str):
            model_id = model_id.removeprefix("models/").strip()
            if model_id and len(model_id) <= 200:
                models.append(model_id)
    models = sorted(set(models), key=str.casefold)
    if not models:
        raise HTTPException(status_code=502, detail="La réponse du fournisseur ne contient aucun modèle utilisable.")
    return {"models": models}


@app.put("/api/settings/provider")
def update_provider_settings(req: ProviderSettingsRequest, origin: Optional[str] = Header(default=None, alias="Origin")):
    if origin:
        try:
            parsed_origin = urlsplit(origin)
            local_origin = parsed_origin.scheme in {"http", "https"} and parsed_origin.hostname in {"127.0.0.1", "localhost", "::1"}
        except ValueError:
            local_origin = False
        if not local_origin:
            raise HTTPException(status_code=403, detail="La configuration des clés est réservée à l’interface locale.")
    if req.provider not in PROVIDER_PRESETS:
        raise HTTPException(status_code=400, detail="Fournisseur inconnu.")
    preset = PROVIDER_PRESETS[req.provider]
    base_url = req.base_url.strip().rstrip("/")
    model = req.model.strip() or preset["model"]
    try:
        parsed_url = urlsplit(base_url)
        local_http = parsed_url.scheme == "http" and parsed_url.hostname in {"127.0.0.1", "localhost", "::1"}
        secure_https = parsed_url.scheme == "https" and bool(parsed_url.hostname)
    except ValueError:
        local_http = secure_https = False
        parsed_url = None
    if not (local_http or secure_https) or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
        raise HTTPException(status_code=400, detail="L’URL doit utiliser HTTPS (sauf pour un serveur local).")
    if not model or len(model) > 200:
        raise HTTPException(status_code=400, detail="URL ou nom de modèle invalide.")
    key = (req.api_key or "").strip()
    if key and not req.clear_api_key:
        try:
            set_api_key(key, req.provider)
        except Exception as exc:
            logger.exception("Secure API credential save failed for provider=%s", req.provider)
            raise HTTPException(
                status_code=503,
                detail="Impossible d’enregistrer la clé dans le coffre sécurisé Windows. Consultez le journal du serveur.",
            ) from exc
    if req.clear_api_key:
        delete_api_key(req.provider)
    try:
        has_key = bool(get_api_key(req.provider))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Le coffre-fort système n’est pas disponible.") from exc
    if req.enabled and not has_key:
        raise HTTPException(status_code=400, detail="Ajoutez une clé API avant d’activer le fournisseur.")
    state = storage.get_setting("api_providers", {}) or {}
    configs = state.get("configs", {})
    legacy = storage.get_setting("api_provider", {}) or {}
    if not configs and legacy:
        configs[legacy.get("provider", "openai-compatible")] = legacy
    selected_models = list(dict.fromkeys(name.strip() for name in req.models if isinstance(name, str) and name.strip() and len(name.strip()) <= 200))
    if not selected_models:
        selected_models = [model]
    if len(selected_models) > 500:
        raise HTTPException(status_code=400, detail="Sélectionnez au maximum 500 modèles.")
    if model not in selected_models:
        model = selected_models[0]
    configs[req.provider] = {"base_url": base_url, "model": model, "models": selected_models, "type": preset["type"]}
    active_provider = req.provider if req.enabled else (None if state.get("active_provider", legacy.get("provider")) == req.provider else state.get("active_provider"))
    storage.set_setting("api_providers", {"active_provider": active_provider, "configs": configs})
    return get_provider_settings(req.provider)


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
    """L'ancien protocole d'appairage attend le contrat GET découvrable d'Etu2."""
    raise HTTPException(status_code=501, detail="Appairage Etu2 indisponible avant la publication de son contrat d'API")


@app.get("/api/status")
def server_status():
    hw = scan_hardware()
    installed = downloader.list_installed_models()
    beacon_online = wizard_manager.is_beacon_online()

    active_model = None
    active_engine = None
    active_mmproj = None
    active_context = None
    model_ready = False
    model_state = "offline"
    if beacon_online:
        try:
            req_b = urllib.request.Request("http://127.0.0.1:11343/api/status")
            with urllib.request.urlopen(req_b, timeout=2) as resp:
                bdata = json.loads(resp.read().decode("utf-8"))
                active_model = bdata.get("active_model")
                active_engine = bdata.get("active_engine")
                active_mmproj = bdata.get("active_mmproj")
                active_context = bdata.get("active_context")
                model_state = bdata.get("status", "unknown")
                model_ready = bdata.get("status") == "running" and bool(active_model)
        except Exception:
            pass

    saved_selected_model = storage.get_setting("selected_model")
    if not active_model and saved_selected_model:
        active_model = saved_selected_model
    elif not active_model and installed:
        active_model = installed[0].replace(".gguf", "")

    catalog_items = get_catalog_with_status(hw, installed)
    installed_catalog = [m for m in catalog_items if m.get("is_installed")]
    provider_state = storage.get_setting("api_providers", {}) or {}
    active_provider = provider_state.get("active_provider")
    active_remote_model = next((model for model in get_configured_api_models() if model["provider"] == active_provider), None)

    return {
        "app_name": "ETU-AI",
        "status": "online",
        "setup_completed": wizard_manager.is_completed(),
        "beacon_online": beacon_online,
        "active_model": active_model,
        "active_remote_model": active_remote_model,
        "active_engine": active_engine,
        "active_mmproj": active_mmproj,
        "active_context": active_context,
        "model_ready": model_ready,
        "model_state": model_state,
        "paired_with_etu": bool(bridge.record and bridge.record["grants"]),
        "etu2_available": True,
        "installed_models_count": len(installed),
        "installed_models": installed_catalog,
        "remote_models": get_configured_api_models(),
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
        response = FileResponse(index_path, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})
        response.set_cookie("etu_local_admin", bridge.admin_token, httponly=True, samesite="strict")
        return response
    return JSONResponse({"status": "ETU-AI API active", "ui": "loading"})
