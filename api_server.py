"""
Serveur API FastAPI principal pour ETU-AI.
Expose les endpoints de chat SSE, de gestion des modèles Hugging Face, de persistance SQLite,
du handshake avec etu-aix.alwaysdata.net et sert l'interface web statique.
"""

import json
import asyncio
import httpx
import hashlib
import struct
import base64
import logging
import os
import platform
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import UUID, uuid4
from urllib.error import HTTPError, URLError
import urllib.request
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import sys
BASE_DIR = Path(__file__).resolve().parent
AI_SERVER_DIR = BASE_DIR / "ai-server"
if str(AI_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(AI_SERVER_DIR))

from chat.sse_handler import ChatStreamHandler
from chat.provider_usage import get_rate_limit
from chat import chatgpt_auth
from chat.attachments import prepare_attachments, load_attachments
from configs import init_file_logger
from install import scan_hardware
from configs import CONFIG_FILE, DEFAULT_CONFIG, STATE_FILE, load_config
from llama_setup import setup_llama
from release_catalog import backend_for_variant, release_variants
from setup_utils import atomic_json, file_lock, read_json

init_file_logger("api_server.log")
from models_mgr.catalog import get_catalog_with_status
from models_mgr.downloader import ModelDownloader
from models_mgr.native import NativeInstaller
from models_mgr.projectors import ProjectorManager, is_projector, known_text_only
from models_mgr.hf_client import hf_client, PARAMETER_RANGES
from storage.db import StorageManager
from storage.api_credentials import delete_api_key, get_api_key, set_api_key
from storage.state_manager import state_manager
from storage.model_performance import append_model_performance, read_model_performance
from wiki.service import WikiService
from wiki.api import router_for as wiki_router
from tools.executor import ToolExecutor
from integration.api import router as etu_router, LOCAL_ORIGINS, admin as require_local_admin
from integration.bridge import bridge, ETU_ORIGINS, ALLOW_LOCALHOST_ORIGINS, LOCALHOST_ORIGIN_REGEX, is_etu_origin
from integration.updates import latest_app_release, installed_version, is_newer_version
from companion import SHORTCUTS, DEFAULT_SHORTCUT

from setup_wizard import wizard_manager

logger = logging.getLogger(__name__)

UI_DIR = BASE_DIR / "ui"

storage = StorageManager()
tool_executor = ToolExecutor()
downloader = ModelDownloader(storage=storage)
native_installer = NativeInstaller(downloader)
_server_reinstall_lock = threading.Lock()
_server_reinstall: dict[str, Any] = {"status": "idle", "message": ""}
_inference_condition = threading.Condition()
_inference_reinstall_pending = False
_active_inference_requests = 0
_power_condition = threading.Condition()
_power_state = "awake"
_power_error = ""
_hardware_info_cache: Optional[dict[str, Any]] = None
_hardware_info_cache_lock = threading.Lock()
stream_handler = ChatStreamHandler(storage=storage, tool_executor=tool_executor)
wiki_service = WikiService(BASE_DIR / "storage" / "wiki", storage, busy=lambda: bool(_active_inference_requests) or _power_state != "awake" or _inference_reinstall_pending or _model_activation_lock.locked())
stream_handler.wiki = wiki_service
_active_chat_conversations: set[str] = set()
_model_activation_lock = threading.Lock()

app = FastAPI(title="ETU-AI Core Server", version="1.0.0")
app.state.etu_storage = storage
app.include_router(wiki_router(wiki_service))


@app.on_event("startup")
async def start_wiki():
    await wiki_service.start()


@app.on_event("shutdown")
async def stop_wiki():
    await wiki_service.stop()



def get_hardware_info() -> dict[str, Any]:
    """Return the startup hardware scan instead of probing devices per request."""
    global _hardware_info_cache
    if _hardware_info_cache is None:
        with _hardware_info_cache_lock:
            if _hardware_info_cache is None:
                _hardware_info_cache = scan_hardware()
    return _hardware_info_cache


@app.middleware("http")
async def track_inference_requests(request: Request, call_next):
    """Refuse les nouvelles générations pendant une réinstallation et suit les flux actifs."""
    global _active_inference_requests
    if request.url.path not in {"/api/chat/stream", "/v1/chat/completions"} or request.method != "POST":
        return await call_next(request)
    with _inference_condition:
        if _inference_reinstall_pending:
            return JSONResponse(status_code=503, content={"detail": "Réinstallation du moteur en cours. Réessayez dans un instant."})
        _active_inference_requests += 1
    wiki_service.queue.interrupt()

    try:
        response = await call_next(request)
    except BaseException:
        with _inference_condition:
            _active_inference_requests -= 1
            _inference_condition.notify_all()
        raise

    body_iterator = response.body_iterator

    async def tracked_body():
        global _active_inference_requests
        try:
            async for chunk in body_iterator:
                yield chunk
        finally:
            with _inference_condition:
                _active_inference_requests -= 1
                _inference_condition.notify_all()

    response.body_iterator = tracked_body()
    return response


@app.middleware("http")
async def reject_requests_while_sleeping(request: Request, call_next):
    """Keep the local UI reachable while refusing API traffic during sleep."""
    if request.method != "OPTIONS" and request.url.path.startswith(("/api/", "/v1/")):
        with _power_condition:
            state = _power_state
        if state != "awake" and request.url.path not in {
            "/api/power/state", "/api/power/wake", "/api/power/restart", "/api/power/shutdown",
            "/api/settings/beacon", "/api/settings/beacon/context",
            "/v1/status",
        }:
            return JSONResponse(status_code=503, content={"detail": "Désolé je suis en veille"})
    return await call_next(request)
from integration.remote import router as remote_router, remote as etu_remote
app.include_router(remote_router)
@app.on_event("startup")
async def start_etu_remote():
    await etu_remote.start(app)
@app.on_event("shutdown")
async def stop_etu_remote():
    await etu_remote.stop()

@app.on_event("startup")
def on_startup():
    """Initialisation au démarrage du serveur : enregistre l'état et lance Beacon si setup déjà fait."""
    import threading
    try:
        bridge.identity()
    except Exception:
        # The ETU bridge is optional; an unreadable identity must not prevent
        # the local chat and provider settings API from starting.
        logger.exception("ETU bridge identity is unavailable; continuing without the ETU bridge")
    try:
        state_manager.record_startup()
    except OSError as exc:
        print(f"[!] Horodatage du démarrage non enregistré : {exc}")
    try:
        hw = scan_hardware()
        global _hardware_info_cache
        _hardware_info_cache = hw
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

    # Garder MAIA-Beacon actif après la configuration, et le relancer s'il tombe.
    if wizard_manager.is_completed():
        threading.Thread(target=maintain_beacon, name="maia-beacon-supervisor", daemon=True).start()


def maintain_beacon():
    """Start Beacon after setup and recover it if its process exits unexpectedly."""
    while True:
        try:
            with _power_condition:
                app_is_awake = _power_state == "awake"
            if app_is_awake and wizard_manager.is_beacon_enabled() and wizard_manager.is_completed() and not wizard_manager.is_beacon_online():
                wizard_manager.ensure_beacon_running()
        except Exception:
            logger.exception("MAIA-Beacon supervision failed")
        time.sleep(10)

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
from integration.workshop import router as workshop_router
app.include_router(workshop_router)


@app.get("/api/launch-mode")
def get_launch_mode():
    mode = os.environ.get("ETU_LAUNCH_MODE", "normal").strip().lower()
    if mode not in {"normal", "local", "server"}:
        mode = "normal"
    return {
        "mode": mode,
        "pid": os.getpid(),
        "beacon_host": "0.0.0.0" if mode == "server" else "127.0.0.1",
        "beacon_port": 11343,
        "localhost_origins_allowed": ALLOW_LOCALHOST_ORIGINS,
    }


class CompanionShortcutRequest(BaseModel):
    shortcut: str


@app.get("/api/settings/companion")
def get_companion_settings(request: Request):
    require_local_admin(request)
    shortcut = storage.get_setting("companion_shortcut", DEFAULT_SHORTCUT)
    return {"shortcut": shortcut if shortcut in SHORTCUTS else DEFAULT_SHORTCUT}


@app.put("/api/settings/companion")
def set_companion_settings(request: Request, body: CompanionShortcutRequest):
    require_local_admin(request)
    if body.shortcut not in SHORTCUTS:
        raise HTTPException(status_code=422, detail="Raccourci non pris en charge.")
    storage.set_setting("companion_shortcut", body.shortcut)
    return {"shortcut": body.shortcut}


@app.get("/api/updates/app")
def get_app_release(request: Request):
    require_local_admin(request)
    try:
        result = latest_app_release()
        current = installed_version()
        result["installed_version"] = current
        result["update_available"] = is_newer_version(result.get("version", ""), current)
        result["choice"] = storage.get_setting("app_update_choice_" + result.get("version", ""), "")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class UpdateChoiceRequest(BaseModel):
    version: str = Field(pattern=r"^v?\d+(?:\.\d+){1,3}$", max_length=128)
    choice: Literal["open", "later"]


@app.put("/api/updates/app/choice")
def set_update_choice(request: Request, body: UpdateChoiceRequest):
    require_local_admin(request)
    storage.set_setting("app_update_choice_" + body.version, body.choice)
    return {"version": body.version, "choice": body.choice}


def _startup_command() -> str:
    python = BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
    if not python.is_file():
        raise ValueError("Environnement Windows introuvable. Installez ETU-AI avant d’activer cette option.")
    return subprocess.list2cmdline([str(python), str(BASE_DIR / "run_launcher.py"), "--mode", "local"])


@app.get("/api/settings/startup")
def get_startup_setting(request: Request):
    require_local_admin(request)
    if os.name != "nt":
        return {"supported": False, "enabled": False}
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            value, _ = winreg.QueryValueEx(key, "ETU-AI")
        enabled = value == _startup_command()
    except (FileNotFoundError, ValueError):
        enabled = False
    return {"supported": True, "enabled": enabled}


class StartupRequest(BaseModel):
    enabled: bool


@app.put("/api/settings/startup")
def set_startup_setting(request: Request, body: StartupRequest):
    require_local_admin(request)
    if os.name != "nt":
        raise HTTPException(status_code=400, detail="Disponible uniquement sous Windows.")
    import winreg
    try:
        command = _startup_command() if body.enabled else None
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            if body.enabled:
                winreg.SetValueEx(key, "ETU-AI", 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, "ETU-AI")
                except FileNotFoundError:
                    pass
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"supported": True, "enabled": body.enabled}


@app.get("/api/power/state")
def get_power_state(request: Request):
    require_local_admin(request)
    with _power_condition:
        return {"state": _power_state, "error": _power_error, "mode": get_launch_mode()["mode"]}


def _finish_sleep() -> None:
    global _power_state, _power_error
    try:
        with _inference_condition:
            while _active_inference_requests:
                _inference_condition.wait(timeout=1)
        wizard_manager.stop_beacon_listener()
        with _power_condition:
            _power_state = "sleeping"
            _power_condition.notify_all()
    except Exception as exc:
        logger.exception("Cannot put the inference backend to sleep")
        with _power_condition:
            _power_state = "awake"
            _power_error = str(exc)
            _power_condition.notify_all()


@app.post("/api/power/sleep")
def sleep_app(request: Request):
    global _power_state, _power_error
    require_local_admin(request)
    with _power_condition:
        if _power_state != "awake":
            raise HTTPException(status_code=409, detail="Une transition d’alimentation est déjà en cours")
        if _inference_reinstall_pending:
            raise HTTPException(status_code=409, detail="Attendez la fin de la réinstallation")
        _power_state = "entering_sleep"
        _power_error = ""
    threading.Thread(target=_finish_sleep, daemon=True).start()
    return {"state": "entering_sleep"}


@app.post("/api/power/wake")
def wake_app(request: Request):
    global _power_state, _power_error
    require_local_admin(request)
    with _power_condition:
        while _power_state == "entering_sleep":
            _power_condition.wait(timeout=1)
        if _power_state == "awake":
            return {"state": "awake", "woke": False}
        if _power_state != "sleeping":
            raise HTTPException(status_code=409, detail="Une transition d’alimentation est déjà en cours")
        _power_state = "waking"
    try:
        if wizard_manager.is_completed() and wizard_manager.is_beacon_enabled():
            if not wizard_manager.ensure_beacon_running():
                raise RuntimeError("Impossible de démarrer le moteur d’inférence")
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and not wizard_manager.is_beacon_online():
                time.sleep(0.5)
            if not wizard_manager.is_beacon_online():
                raise RuntimeError("Le moteur d’inférence ne répond pas")
    except Exception as exc:
        logger.exception("Cannot wake the inference backend")
        with _power_condition:
            _power_state = "sleeping"
            _power_error = str(exc)
            _power_condition.notify_all()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    with _power_condition:
        _power_state = "awake"
        _power_error = ""
        _power_condition.notify_all()
    return {"state": "awake", "woke": True}


class RestartRequest(BaseModel):
    mode: Literal["normal", "local", "server"]


def _end_app() -> None:
    with _inference_condition:
        while _active_inference_requests:
            _inference_condition.wait(timeout=1)
    try:
        wizard_manager.stop_beacon_listener()
    except Exception:
        logger.exception("Could not stop Beacon during application shutdown")
    time.sleep(0.35)
    app.state.uvicorn_server.should_exit = True


@app.post("/api/power/restart")
def restart_app(request: Request, body: RestartRequest):
    global _power_state
    require_local_admin(request)
    if not getattr(app.state, "uvicorn_server", None):
        raise HTTPException(status_code=409, detail="Redémarrage disponible depuis le lanceur ETU-AI")
    with _power_condition:
        if _power_state in {"entering_sleep", "waking", "shutting_down"} or _inference_reinstall_pending:
            raise HTTPException(status_code=409, detail="Une autre opération est en cours")
        previous_state = _power_state
        _power_state = "shutting_down"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [sys.executable, str(BASE_DIR / "restart_app.py"), body.mode],
            cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags,
        )
    except OSError as exc:
        with _power_condition:
            _power_state = previous_state
        raise HTTPException(status_code=500, detail=f"Redémarrage impossible : {exc}") from exc
    threading.Thread(target=_end_app, daemon=True).start()
    return {"state": "restarting", "mode": body.mode, "old_pid": os.getpid()}


@app.post("/api/power/shutdown")
def shutdown_app(request: Request):
    global _power_state
    require_local_admin(request)
    if not getattr(app.state, "uvicorn_server", None):
        raise HTTPException(status_code=409, detail="Arrêt disponible depuis le lanceur ETU-AI")
    with _power_condition:
        if _power_state in {"entering_sleep", "waking", "shutting_down"} or _inference_reinstall_pending:
            raise HTTPException(status_code=409, detail="Une autre opération est en cours")
        _power_state = "shutting_down"
    threading.Thread(target=_end_app, daemon=True).start()
    return {"state": "shutting_down"}


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
    backend: Literal["auto", "cpu", "cuda", "vulkan", "metal", "npu"] = "auto"
    selected_variant: Optional[str] = None
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
    edit_message_id: Optional[str] = None
    web_search: bool = False
    reasoning_effort: Optional[str] = Field(default=None, min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_-]*$")


class EditMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100000)


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
    reasoning_efforts: dict[str, list[str]] = Field(default_factory=dict)
    api_key: Optional[str] = None
    clear_api_key: bool = False
    auth_method: Literal["account", "api_key"] = "api_key"


class ProviderModelsRequest(BaseModel):
    provider: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    auth_method: Literal["account", "api_key"] = "api_key"


class HardwarePreferenceRequest(BaseModel):
    preference: Literal["cpu", "gpu", "npu", "auto"]


class BeaconEnabledRequest(BaseModel):
    enabled: bool


class BeaconContextRequest(BaseModel):
    context_size: int = Field(ge=512, le=262144, multiple_of=512)


PROVIDER_PRESETS = {
    "openai-compatible": {"label": "Autre API compatible OpenAI", "type": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "openai": {"label": "OpenAI", "type": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "chatgpt": {"label": "ChatGPT", "type": "chatgpt", "base_url": "https://api.openai.com/v1", "model": ""},
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


class NativeDownloadRequest(DownloadRequest):
    model_format: Literal["openvino", "onnx"]


class ModelSelectRequest(BaseModel):
    model: str
    context_size: Optional[int] = None
    source: str = "local"
    provider: Optional[str] = None


class ModelPerformanceRequest(BaseModel):
    model_id: str = Field(min_length=1, max_length=300)
    model_name: str = Field(min_length=1, max_length=240)
    source: str = Field(min_length=1, max_length=10)
    total_ms: int = Field(ge=0, le=86_400_000)
    ttft_ms: Optional[int] = Field(default=None, ge=0, le=86_400_000)
    load_ms: Optional[int] = Field(default=None, ge=0, le=86_400_000)
    prompt_tokens: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    output_tokens: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    tokens_per_second: Optional[float] = Field(default=None, ge=0, le=100_000)
    measurement: str = Field(min_length=1, max_length=12)
    hardware: Optional[dict[str, Any]] = None


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
    if req.edit_message_id:
        if not req.conversation_id:
            raise HTTPException(status_code=422, detail="La modification doit cibler un message existant.")
        messages = storage.get_messages(conv_id)
        if not messages or messages[-1]["id"] != req.edit_message_id or messages[-1]["role"] != "user":
            raise HTTPException(status_code=409, detail="Ce message n’est plus le dernier tour modifiable.")
        existing_message = load_attachments(storage, [messages[-1]])[0]
        combined = [*(existing_message.get("attachments") or []), *attachments]
        if len(combined) > 6 or sum(int(item.get("size") or 0) for item in combined) > 20 * 1024 * 1024:
            raise HTTPException(status_code=422, detail="6 fichiers maximum et 20 Mo au total pour ce message.")
    _active_chat_conversations.add(conv_id)

    async def guarded_stream():
        stream = stream_handler.stream_chat(
            conv_id, req.message, req.model or profile.get("model"), profile,
            attachments=attachments, request_id=request_id,
            existing_user_message_id=req.edit_message_id,
            web_search=req.web_search,
            reasoning_effort=req.reasoning_effort,
        )
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


@app.post("/api/conversations/{conv_id}/etu-confirmations/{token}")
async def confirm_etu_action(conv_id: str, token: str, body: dict[str, Any]):
    from chat.confirmations import decide
    if not storage.get_conversation(conv_id) or not decide(token, conv_id, body.get("approved") is True, body.get("friend_ids", body.get("friend_id")), body.get("event")):
        raise HTTPException(404, "Confirmation expirée")
    return {"ok": True}


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


@app.put("/api/conversations/{conv_id}/messages/{message_id}")
def edit_conversation_message(conv_id: str, message_id: str, req: EditMessageRequest):
    if not storage.get_conversation(conv_id):
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    if conv_id in _active_chat_conversations:
        raise HTTPException(status_code=409, detail="Arrêtez la génération avant de modifier un message.")
    try:
        removed = storage.edit_user_message(conv_id, message_id, req.content)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"success": True, "message_id": message_id, "removed_messages": removed}


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str):
    success = storage.delete_conversation(conv_id)
    return {"success": success}


@app.delete("/api/conversations")
def delete_all_conversations():
    deleted = storage.delete_all_conversations()
    return {"success": True, "deleted": deleted}


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
        uses_chatgpt_account = config.get("type") == "chatgpt" or provider_id == "chatgpt"
        if uses_chatgpt_account:
            try:
                has_account = bool(chatgpt_auth.status()["active"])
            except Exception:
                has_account = False
            if not has_account:
                continue
        preset = PROVIDER_PRESETS.get(provider_id, {})
        for model_name in selected_models:
            model_entry = {
                "id": f"api:{provider_id}:{model_name}",
                "model": model_name,
                "name": model_name,
                "provider": provider_id,
                "provider_label": preset.get("label", provider_id),
                "provider_type": config.get("type", preset.get("type", "openai")),
                "source": "api",
            }
            effort_map = config.get("reasoning_efforts", {})
            efforts = effort_map.get(model_name) if isinstance(effort_map, dict) else effort_map
            if isinstance(efforts, list):
                clean_efforts = _clean_reasoning_efforts(efforts)
                if clean_efforts:
                    model_entry["reasoning_efforts"] = clean_efforts
            models.append(model_entry)
    return models


def _clean_reasoning_efforts(values):
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value)))[:12]


def _model_reasoning_efforts(item):
    if not isinstance(item, dict):
        return []
    capabilities = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
    reasoning = item.get("reasoning") if isinstance(item.get("reasoning"), dict) else {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    capability_reasoning = capabilities.get("reasoning") if isinstance(capabilities.get("reasoning"), dict) else {}
    for value in (item.get("reasoning_efforts"), item.get("supported_reasoning_efforts"), capabilities.get("reasoning_efforts"), capability_reasoning.get("efforts"), reasoning.get("efforts"), reasoning.get("supported_efforts"), metadata.get("reasoning_efforts")):
        efforts = _clean_reasoning_efforts(value)
        if efforts:
            return efforts
    return []


@app.get("/api/models/catalog")
def get_catalog():
    hw = get_hardware_info()
    installed = set(downloader.list_installed_models())
    # Le catalogue local doit rester disponible même si Hugging Face est lent.
    # L'exploration distante possède son propre onglet et son propre endpoint.
    base_catalog = get_catalog_with_status(hw, list(installed))
    projectors = ProjectorManager(storage, downloader, hf_client)
    for item in base_catalog:
        item["mmproj"] = projectors.describe(item["filename"])

    return {
        "catalog": base_catalog,
        "native_models": native_installer.installed(),
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
    publisher: str = "",
    parameter_range: str = "",
    trusted_only: bool = False,
    hardware_filter: str = "",
    explorer: bool = False,
    model_format: str = "auto",
    precision: str = "",
):
    """Recherche GGUF et découverte des exports OpenVINO/ONNX publics."""
    hw = get_hardware_info()
    installed = set(downloader.list_installed_models())
    if publisher not in {"", "mistralai", "Qwen", "Alibaba-NLP", "deepseek-ai", "meta-llama"}:
        raise HTTPException(status_code=422, detail="Éditeur inconnu")
    if parameter_range and parameter_range not in PARAMETER_RANGES:
        raise HTTPException(status_code=422, detail="Filtre de paramètres inconnu")
    if hardware_filter not in {"", "cpu", "gpu", "npu"}:
        raise HTTPException(status_code=422, detail="Filtre matériel inconnu")
    if model_format not in {"auto", "gguf", "openvino", "onnx", "npu"}:
        raise HTTPException(status_code=422, detail="Format de modèle inconnu")
    if precision not in {"", "int4", "int8", "fp16", "bf16", "fp32"}:
        raise HTTPException(status_code=422, detail="Quantification inconnue")
    resolved_format = ("npu" if hardware_filter == "npu" else "gguf") if model_format == "auto" else model_format
    if resolved_format != "gguf":
        results = hf_client.search_npu_models(
            query=q, model_format=resolved_format, limit=max(1, min(limit, 51)),
            parameter_range=parameter_range, trusted_only=trusted_only,
            publisher=publisher, only_compatible=only_compatible,
            precision=precision,
        )
    else:
        results = hf_client.get_all_compatible_models(
            hardware_info=hw,
            query=q,
            limit=max(1, min(limit, 51)),
            only_compatible=only_compatible,
            publisher=publisher,
            parameter_range=parameter_range,
            trusted_only=trusted_only,
            hardware_filter=hardware_filter,
            preserve_order=explorer,
        )
    installed_native = {model["id"] for model in native_installer.installed()}
    for m in results:
        m["installed_filenames"] = [quant["filename"] for quant in m.get("quantizations", []) if quant.get("filename") in installed]
        if m.get("format") in {"openvino", "onnx"}:
            m["installed_filenames"] = [quant["filename"] for quant in m.get("quantizations", []) if quant.get("id") in installed_native]
        m["is_installed"] = bool(m["installed_filenames"])
    return {
        "query": q,
        "model_format": resolved_format,
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
        m["installed_filenames"] = [quant["filename"] for quant in m.get("quantizations", []) if quant.get("filename") in installed]
        m["is_installed"] = bool(m["installed_filenames"])
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


@app.post("/api/models/native/download")
def download_native_model(req: NativeDownloadRequest):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", req.repo):
        raise HTTPException(status_code=422, detail="Dépôt Hugging Face invalide")
    try:
        variants = hf_client.get_native_variants(req.repo, req.model_format)
        variant = next((item for item in variants if item["filename"] == req.filename), None)
        if not variant:
            raise HTTPException(status_code=404, detail="Variante absente du dépôt")
        if any(item["id"] == variant["id"] for item in native_installer.installed()):
            raise HTTPException(status_code=409, detail="Cette variante est déjà installée")
        return {"task_id": native_installer.start(req.repo, variant), "status": "started"}
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/models/native/{model_id}")
def uninstall_native_model(model_id: str):
    try:
        native_installer.remove(model_id)
        return {"success": True}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/models/import")
async def import_local_model(request: Request, filename: str):
    """Stream a local GGUF into the model library without buffering it in memory."""
    try:
        downloader.validate_reference("local/import", filename)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if is_projector(filename):
        raise HTTPException(status_code=422, detail="Les projecteurs mmproj/projector se gèrent dans Vision / mmproj.")
    destination = downloader.models_dir / filename
    if destination.exists():
        raise HTTPException(status_code=409, detail="Un modèle portant ce nom est déjà installé.")
    temporary = downloader.models_dir / f"model-import-{uuid4().hex}.part"
    limit = 64 * 1024 ** 3
    size = 0
    header = bytearray()
    try:
        with temporary.open("xb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail="64 Go maximum par modèle importé.")
                header.extend(chunk[:max(0, 8 - len(header))])
                await asyncio.to_thread(output.write, chunk)
        if len(header) < 8 or header[:4] != b"GGUF" or struct.unpack_from("<I", header, 4)[0] not in {2, 3}:
            raise HTTPException(status_code=422, detail="Le fichier sélectionné n'est pas un GGUF valide.")
        if destination.exists():
            raise HTTPException(status_code=409, detail="Un modèle portant ce nom est déjà installé.")
        temporary.replace(destination)
        return {"success": True, "filename": filename}
    finally:
        temporary.unlink(missing_ok=True)


@app.get("/api/models/tasks/{task_id}")
def get_download_task(task_id: str):
    task = downloader.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tâche introuvable")
    return task


@app.get("/api/models/tasks")
def list_download_tasks():
    return {"tasks": downloader.list_tasks()}


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


@app.delete("/api/models/mmproj/{filename}")
def uninstall_model_projector(filename: str):
    """Remove an installed projector and every association that references it."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.gguf", filename, re.IGNORECASE) or not is_projector(filename):
        raise HTTPException(status_code=422, detail="Projecteur local invalide")
    model_path = downloader.models_dir / filename
    if model_path.parent.resolve() != downloader.models_dir.resolve() or model_path.is_symlink():
        raise HTTPException(status_code=422, detail="Projecteur local invalide")
    if not model_path.is_file():
        raise HTTPException(status_code=404, detail="Projecteur introuvable")
    if _active_chat_conversations:
        raise HTTPException(status_code=409, detail="Arrêtez la génération avant de désinstaller ce projecteur.")

    try:
        status_url = "http://127.0.0.1:11343/api/status"
        with urllib.request.urlopen(status_url, timeout=2) as response:
            beacon_status = json.loads(response.read().decode("utf-8"))
        if beacon_status.get("status") == "running" and beacon_status.get("active_mmproj") == filename:
            stop = urllib.request.Request(
                status_url.replace("/api/status", "/api/stop"), data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(stop, timeout=15):
                pass
    except (HTTPError, URLError, OSError, ValueError) as exc:
        if isinstance(exc, HTTPError):
            raise HTTPException(status_code=503, detail="Impossible d’arrêter le projecteur actif.") from exc

    try:
        assignments = storage.get_setting("mmproj_assignments", {}) or {}
        affected_models = [model for model, association in assignments.items() if association.get("local_filename") == filename]
        manager = ProjectorManager(storage, downloader, hf_client)
        for model in affected_models:
            manager.assign(model, None)
        model_path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Impossible de supprimer le fichier du projecteur.") from exc
    return {"success": True, "filename": filename}


@app.delete("/api/models/{filename}")
def uninstall_model(filename: str):
    """Remove an installed local GGUF model after safely stopping it if active."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.gguf", filename, re.IGNORECASE) or is_projector(filename):
        raise HTTPException(status_code=422, detail="Modèle local invalide")
    model_path = downloader.models_dir / filename
    if model_path.parent.resolve() != downloader.models_dir.resolve() or model_path.is_symlink():
        raise HTTPException(status_code=422, detail="Modèle local invalide")
    if not model_path.is_file():
        raise HTTPException(status_code=404, detail="Modèle introuvable")
    if _active_chat_conversations:
        raise HTTPException(status_code=409, detail="Arrêtez la génération avant de désinstaller ce modèle.")

    model_name = filename[:-5]
    beacon_status = None
    try:
        with urllib.request.urlopen("http://127.0.0.1:11343/api/status", timeout=2) as response:
            beacon_status = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, ValueError):
        pass
    if beacon_status and beacon_status.get("active_model") == model_name and beacon_status.get("status") == "running":
        try:
            stop = urllib.request.Request(
                "http://127.0.0.1:11343/api/stop", data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(stop, timeout=15):
                pass
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Impossible d’arrêter le modèle en cours.") from exc

    try:
        assignments = storage.get_setting("mmproj_assignments", {}) or {}
        if filename in assignments:
            ProjectorManager(storage, downloader, hf_client).assign(filename, None)
        model_path.unlink()
        if storage.get_setting("selected_model") == model_name:
            storage.set_setting("selected_model", None)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Impossible de supprimer le fichier du modèle.") from exc
    return {"success": True, "filename": filename}


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


def _beacon_has_model(status: dict[str, Any], model_name: str) -> bool:
    available = status.get("available_models")
    if not isinstance(available, list):
        return True  # Older Beacon versions do not expose their model inventory here.
    names = {
        Path(item).stem.casefold()
        for item in available
        if isinstance(item, str) and item.strip()
    }
    return model_name.casefold() in names


def activate_local_model_for_chat(model_name: str, needs_vision: bool = False) -> dict[str, Any]:
    """Load the queued local model when a chat request first needs it."""
    clean_model = model_name[:-5] if model_name.lower().endswith(".gguf") else model_name
    if f"{clean_model}.gguf" not in downloader.list_installed_models():
        raise RuntimeError("Le modèle sélectionné n'est plus installé")
    with _model_activation_lock:
        try:
            # Vision projectors are only needed when the current request contains
            # images. Loading one for ordinary text can consume substantial VRAM.
            mmproj = ProjectorManager(storage, downloader, hf_client).selected(f"{clean_model}.gguf") if needs_vision else None
            inference_config = load_config()
            context_size = int(inference_config.get("context_size", 4096))
            if inference_config.get("backend") == "npu":
                context_size = min(context_size, 1024)
            if not wizard_manager.is_beacon_online():
                if not wizard_manager.ensure_beacon_running():
                    raise RuntimeError("Impossible de démarrer MAIA-Beacon")
                # A cold Windows startup can take longer while the supervisor
                # validates the OpenVINO runtime and discovers the NPU. Keep
                # the UI request alive long enough to receive the actual result.
                startup_deadline = time.monotonic() + 300
                while time.monotonic() < startup_deadline and not wizard_manager.is_beacon_online():
                    time.sleep(0.5)
                if not wizard_manager.is_beacon_online():
                    raise RuntimeError("MAIA-Beacon ne répond pas")

            status_url = "http://127.0.0.1:11343/api/status"
            with urllib.request.urlopen(status_url, timeout=3) as response:
                current = json.loads(response.read().decode("utf-8"))
            if not _beacon_has_model(current, clean_model):
                raise RuntimeError(
                    f"Le Beacon connecté ne connaît pas {clean_model}.gguf. "
                    "Il utilise probablement un autre dossier de modèles."
                )
            same_model = current.get("status") == "running" and current.get("active_model") == clean_model
            is_npu_engine = str(current.get("active_engine", "")).casefold() == "npu"
            same_thinking = is_npu_engine or (
                current.get("active_thinking") is True
                and current.get("active_thinking_effort") == "medium"
            )
            try:
                same_context = is_npu_engine or int(current.get("active_context") or 0) == context_size
            except (TypeError, ValueError):
                same_context = False
            same_mmproj = current.get("active_mmproj") == mmproj
            if same_model and same_mmproj and same_context and same_thinking:
                return current
            if same_model and (not same_mmproj or not same_context or not same_thinking):
                stop_request = urllib.request.Request(status_url.replace("/api/status", "/api/stop"), data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(stop_request, timeout=10):
                    pass

            selection = {
                "model": clean_model,
                "context_size": context_size,
                "thinking": True,
                "thinking_effort": "medium",
                "mmproj": mmproj or "__etu_no_mmproj__.gguf",
            }
            req = urllib.request.Request(
                "http://127.0.0.1:11343/api/select", data=json.dumps(selection).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))
            if result.get("success") is False:
                raise RuntimeError(result.get("error") or "Beacon a refusé le modèle")

            # OpenVINO may spend several minutes compiling the first stateless
            # NPU graph; keep the request alive through that cold start.
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                with urllib.request.urlopen(status_url, timeout=3) as response:
                    status = json.loads(response.read().decode("utf-8"))
                if status.get("status") == "error":
                    raise RuntimeError(status.get("error_message") or "Le chargement du modèle a échoué")
                if status.get("status") == "running" and status.get("active_model") == clean_model:
                    if status.get("active_mmproj") != mmproj:
                        raise RuntimeError("Beacon n'a pas chargé le projecteur associé à cette variante")
                    return status
                time.sleep(0.5)
            raise RuntimeError("Délai de chargement du modèle dépassé")
        except (HTTPException, RuntimeError):
            raise
        except Exception as exc:
            raise RuntimeError(f"Beacon indisponible : {exc}") from exc


stream_handler.local_model_selector = activate_local_model_for_chat


@app.post("/api/models/select")
def select_model(req: ModelSelectRequest):
    """Choisit le prochain modèle ; un modèle local ne se charge qu'au prochain message."""
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
            uses_chatgpt_account = config.get("type") == "chatgpt" or req.provider == "chatgpt"
            if uses_chatgpt_account and not chatgpt_auth.status()["active"]:
                raise HTTPException(status_code=400, detail="Connectez le compte ou ajoutez une clé API pour ce fournisseur")
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
    storage.set_setting("selected_model", clean_model)
    provider_state = storage.get_setting("api_providers", {}) or {}
    if provider_state:
        provider_state["active_provider"] = None
        storage.set_setting("api_providers", provider_state)
    legacy_provider = storage.get_setting("api_provider", {}) or {}
    if legacy_provider.get("enabled"):
        legacy_provider["enabled"] = False
        storage.set_setting("api_provider", legacy_provider)
    return {"success": True, "queued": True, "active_model": clean_model, "active_mmproj": mmproj}


@app.post("/api/models/performance")
def save_model_performance(req: ModelPerformanceRequest):
    """Append anonymized generation timings to a local, git-ignored JSONL file."""
    if req.source not in {"local", "api"} or req.measurement not in {"server", "estimated", "activation"}:
        raise HTTPException(status_code=422, detail="Source ou type de mesure invalide")
    entry = req.dict()
    entry["recorded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        append_model_performance(entry)
    except OSError as exc:
        logger.warning("Impossible d'enregistrer les performances du modèle: %s", exc)
        raise HTTPException(status_code=500, detail="Enregistrement local des performances impossible") from exc
    return {"recorded": True}


@app.get("/api/models/performance")
def get_model_performance(model_id: Optional[str] = None, model_name: Optional[str] = None, limit: int = 100):
    """Return local measurements for the requested model; no chat content is stored."""
    if not model_id and not model_name:
        raise HTTPException(status_code=422, detail="Un identifiant ou un nom de modèle est requis")
    return {"measurements": read_model_performance(model_id, model_name, limit)}


# -----------------------------------------------------------------------------
# Setup Wizard Endpoints
# -----------------------------------------------------------------------------
@app.get("/api/setup/status")
def get_setup_status():
    return wizard_manager.get_status()


@app.get("/api/settings/hardware")
def get_hardware_preference():
    preference = storage.get_setting("hardware_preference", "auto")
    if preference not in {"cpu", "auto", "gpu", "npu"}:
        preference = "auto"
    hw = get_hardware_info()
    return {"preference": preference, "has_npu": bool(hw.get("npus")), "has_gpu": bool(hw.get("gpus")), "beacon_enabled": wizard_manager.is_beacon_enabled()}


def get_beacon_settings() -> dict[str, Any]:
    enabled = wizard_manager.is_beacon_enabled()
    online = wizard_manager.is_beacon_online()
    launch_mode = get_launch_mode()["mode"]
    config = read_json(AI_SERVER_DIR / "config.json")
    beacon_status: dict[str, Any] = {}
    if online:
        try:
            with urllib.request.urlopen("http://127.0.0.1:11343/api/status", timeout=2) as response:
                beacon_status = json.loads(response.read().decode("utf-8"))
        except Exception:
            beacon_status = {}
    process = wizard_manager.beacon_process
    with _power_condition:
        power_state = _power_state
    return {
        "enabled": enabled,
        "online": online,
        "setup_completed": wizard_manager.is_completed(),
        "status": beacon_status.get("status", "offline" if not online else "unknown"),
        "active_model": beacon_status.get("active_model"),
        "active_engine": beacon_status.get("active_engine"),
        "active_context": beacon_status.get("active_context"),
        "context_size": int(config.get("context_size", 4096)),
        "host": "0.0.0.0" if launch_mode == "server" else "127.0.0.1",
        "port": int(config.get("beacon_port", 11343)),
        "mode": launch_mode,
        "power_state": power_state,
        "backend": config.get("backend", "auto"),
        "pid": process.pid if process and process.poll() is None else None,
    }


@app.get("/api/settings/beacon")
def get_beacon_settings_endpoint(request: Request):
    require_local_admin(request)
    return get_beacon_settings()


@app.put("/api/settings/beacon")
def update_beacon_enabled(request: Request, req: BeaconEnabledRequest):
    require_local_admin(request)
    storage.set_setting("beacon_enabled", req.enabled)
    if req.enabled:
        if wizard_manager.is_completed() and not wizard_manager.ensure_beacon_running():
            raise HTTPException(status_code=503, detail="MAIA-Beacon n’a pas pu démarrer")
    elif not wizard_manager.stop_beacon_listener():
        raise HTTPException(status_code=500, detail="MAIA-Beacon n’a pas pu s’arrêter")
    return get_beacon_settings()


@app.put("/api/settings/beacon/context")
def update_beacon_context(request: Request, req: BeaconContextRequest):
    require_local_admin(request)
    config_path = AI_SERVER_DIR / "config.json"
    config = {**DEFAULT_CONFIG, **read_json(config_path)}
    config["context_size"] = req.context_size
    try:
        atomic_json(config_path, config)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Impossible d’enregistrer la taille du contexte.") from exc
    return get_beacon_settings()


@app.post("/api/settings/beacon/restart")
def restart_beacon(request: Request):
    require_local_admin(request)
    if not wizard_manager.is_beacon_enabled():
        raise HTTPException(status_code=409, detail="MAIA-Beacon est désactivé. Réactivez-le pour le redémarrer.")
    if not wizard_manager.is_completed() and not wizard_manager.is_beacon_online():
        raise HTTPException(status_code=409, detail="MAIA-Beacon n’est pas encore configuré")
    try:
        wizard_manager.stop_beacon_listener()
        if not wizard_manager.ensure_beacon_running():
            raise RuntimeError("MAIA-Beacon n’a pas ouvert son API")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Impossible de redémarrer MAIA-Beacon : {exc}") from exc
    return get_beacon_settings()


@app.put("/api/settings/hardware")
def update_hardware_preference(req: HardwarePreferenceRequest):
    hw = get_hardware_info()
    if req.preference == "npu" and not hw.get("npus"):
        raise HTTPException(status_code=422, detail="Aucun NPU détecté sur cet ordinateur")
    if req.preference == "gpu" and not hw.get("gpus"):
        raise HTTPException(status_code=422, detail="Aucun GPU détecté sur cet ordinateur")
    gpu_backend = hw.get("recommended_backend")
    if req.preference == "gpu" and gpu_backend not in {"cuda", "metal", "vulkan"}:
        raise HTTPException(status_code=422, detail="Aucun backend GPU compatible n’est disponible")
    config = load_config()
    config["backend"] = "cpu" if req.preference == "cpu" else "npu" if req.preference == "npu" else gpu_backend if req.preference == "gpu" else "auto"
    config_path = AI_SERVER_DIR / "config.json"
    try:
        existing = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        existing["backend"] = config["backend"]
        atomic_json(config_path, existing)
        storage.set_setting("hardware_preference", req.preference)
        if wizard_manager.is_beacon_online():
            wizard_manager.stop_beacon_listener()
        if wizard_manager.is_completed() and wizard_manager.is_beacon_enabled() and not wizard_manager.ensure_beacon_running():
            raise RuntimeError("Impossible de redémarrer MAIA-Beacon")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Préférence enregistrée, mais redémarrage de Beacon impossible : {exc}") from exc
    return get_hardware_preference()


@app.get("/api/search/status")
def get_web_search_status():
    from tools.executor import ToolExecutor
    available = ToolExecutor.search_available()
    return {"configured": available, "provider": "DDGS" if available else None}


@app.get("/api/settings/provider")
def get_provider_settings(provider: Optional[str] = None):
    state = storage.get_setting("api_providers", {}) or {}
    configs = state.get("configs", {})
    legacy = storage.get_setting("api_provider", {}) or {}
    if not configs and legacy:
        legacy_id = legacy.get("provider", "openai-compatible")
        configs[legacy_id] = legacy
    legacy_active_provider = state.get("active_provider") or (legacy.get("provider") if legacy.get("enabled") else None)
    if provider is None and legacy_active_provider in {"openai", "chatgpt"}:
        try:
            source_config = configs.get(legacy_active_provider, {})
            auth_method = source_config.get("auth_method", "account" if legacy_active_provider == "chatgpt" and source_config.get("type") == "chatgpt" else "api_key")
            if auth_method == "api_key":
                old_key = get_api_key(legacy_active_provider)
                if old_key:
                    set_api_key(old_key, "openai")
            configs["openai"] = {**configs.get("openai", {}), **source_config, "type": "chatgpt" if auth_method == "account" else "openai", "auth_method": auth_method}
            configs.pop("chatgpt", None)
            if legacy_active_provider == "chatgpt" or state.get("active_provider") == "openai":
                state["active_provider"] = "openai"
                state["configs"] = configs
                storage.set_setting("api_providers", state)
        except Exception:
            logger.exception("Could not consolidate legacy OpenAI and ChatGPT provider settings")
    provider = provider or state.get("active_provider") or legacy.get("provider") or "openai-compatible"
    if provider not in PROVIDER_PRESETS:
        raise HTTPException(status_code=400, detail="Fournisseur inconnu.")
    config = configs.get(provider, {})
    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["custom"])
    try:
        config_url = urlsplit((config.get("base_url") or preset["base_url"]).strip())
        local_no_auth = preset["type"] == "openai" and config_url.scheme == "http" and config_url.hostname in {"127.0.0.1", "localhost", "::1"}
    except ValueError:
        local_no_auth = False
    auth_method = config.get("auth_method", "api_key") if provider == "openai" else (config.get("auth_method", "account") if provider == "chatgpt" else None)
    try:
        has_key = (bool(chatgpt_auth.status()["active"]) if auth_method == "account" else bool(get_api_key(provider))) if provider in {"openai", "chatgpt"} else bool(get_api_key(provider))
        has_openai_api_key = bool(get_api_key("openai")) if provider == "openai" else False
    except Exception:
        has_key = False
        has_openai_api_key = False
    configured = {}
    for key, value in configs.items():
        try:
            configured[key] = {"base_url": value.get("base_url"), "model": value.get("model"), "has_api_key": (bool(chatgpt_auth.status()["active"]) if value.get("auth_method", "api_key") == "account" else bool(get_api_key(key))) if key == "openai" else (bool(chatgpt_auth.status()["active"]) if value.get("type") == "chatgpt" else bool(get_api_key(key))) if key == "chatgpt" else bool(get_api_key(key))}
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
        "has_openai_api_key": has_openai_api_key,
        "auth_method": auth_method,
        "local_no_auth": local_no_auth,
        "auth_optional": preset["type"] != "chatgpt" and auth_method != "account",
        "api_key": "",
    }


@app.get("/api/provider/usage")
async def get_active_provider_usage(request: Request):
    """Return a provider-reported remaining quota when one is available."""
    require_local_admin(request)
    provider_state = storage.get_setting("api_providers", {}) or {}
    provider_id = provider_state.get("active_provider")
    if not provider_id:
        return {"available": False}
    # First use the provider's cached response headers, which are scoped to the
    # actual model request and expire when the reported window resets.
    cached_limit = get_rate_limit(provider_id)
    if cached_limit and provider_id != "openrouter":
        return cached_limit
    if provider_id != "openrouter":
        return cached_limit or {"available": False}
    try:
        api_key = get_api_key("openrouter")
    except Exception:
        api_key = None
    if not api_key:
        return cached_limit or {"available": False}
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            response = await client.get(
                "https://openrouter.ai/api/v1/key",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
            candidate = payload.get("data", {}) if isinstance(payload, dict) else {}
            data = candidate if isinstance(candidate, dict) else {}
        limit = data.get("limit")
        remaining = data.get("limit_remaining")
        if isinstance(limit, (int, float)) and not isinstance(limit, bool) and isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and limit > 0 and remaining >= 0:
            return {"available": True, "kind": "quota", "limit": limit, "remaining": min(remaining, limit), "currency": "USD"}
        # Management keys can read account credits; ordinary keys cannot.
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            response = await client.get(
                "https://openrouter.ai/api/v1/credits",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
            candidate = payload.get("data", {}) if isinstance(payload, dict) else {}
            credits = candidate if isinstance(candidate, dict) else {}
        total = credits.get("total_credits")
        spent = credits.get("total_usage")
        if isinstance(total, (int, float)) and not isinstance(total, bool) and isinstance(spent, (int, float)) and not isinstance(spent, bool):
            return {"available": True, "kind": "balance", "limit": total, "remaining": max(0, total - spent), "currency": "USD"}
    except (httpx.HTTPError, ValueError, TypeError):
        return cached_limit or {"available": False}
    return cached_limit or {"available": False}


@app.get("/api/settings/provider/chatgpt")
def chatgpt_connection_status(request: Request):
    require_local_admin(request)
    return chatgpt_auth.status()


class ChatGPTAccountRequest(BaseModel):
    client_id: Optional[str] = None


@app.post("/api/settings/provider/chatgpt/start")
def start_chatgpt_connection(req: ChatGPTAccountRequest, request: Request):
    require_local_admin(request)
    port = request.scope.get("server", (None, None))[1]
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise HTTPException(status_code=503, detail="Port local de connexion indisponible.")
    try:
        return {"url": chatgpt_auth.start(storage, port, req.client_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/auth/callback", response_class=HTMLResponse)
async def chatgpt_connection_callback(request: Request):
    if request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Connexion locale requise.")
    try:
        await chatgpt_auth.complete(dict(request.query_params))
        result = chatgpt_auth.status()["flow"]
        message = "Connexion terminée. Vous pouvez revenir à ETU-AI." if result["status"] == "success" else "Connexion annulée. Vous pouvez revenir à ETU-AI."
    except Exception:
        logger.exception("ChatGPT sign-in failed")
        chatgpt_auth.fail(request.query_params.get("state", ""), "La connexion ChatGPT a échoué. Réessayez depuis ETU-AI.")
        message = "La connexion a échoué. Vous pouvez revenir à ETU-AI et réessayer."
    success = "terminée" in message
    title = "Connexion réussie" if success else ("Connexion annulée" if "annulée" in message else "Connexion interrompue")
    state = "success" if success else "neutral"
    return HTMLResponse(f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>{title} · ETU-AI</title>
  <style>
    :root {{ color-scheme: dark light; --page: #09090b; --panel: #121215; --text: #f4f4f5; --muted: #a1a1aa; --border: rgba(255,255,255,.1); --accent: #38bdf8; --success: #4ade80; }}
    @media (prefers-color-scheme: light) {{ :root {{ --page: #fcfcfd; --panel: #fff; --text: #18181b; --muted: #62626c; --border: rgba(0,0,0,.1); --accent: #0071e3; --success: #15803d; }} }}
    * {{ box-sizing: border-box; }}
    body {{ min-height: 100vh; margin: 0; display: grid; place-items: center; padding: 24px; background: radial-gradient(ellipse at 50% 0%, color-mix(in srgb, var(--accent) 10%, transparent), transparent 52%), var(--page); color: var(--text); font: 15px/1.6 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    main {{ width: min(100%, 430px); padding: clamp(28px, 7vw, 44px); border: 1px solid var(--border); border-radius: 22px; background: color-mix(in srgb, var(--panel) 92%, transparent); box-shadow: 0 24px 80px rgba(0,0,0,.18); text-align: center; }}
    .brand {{ display: inline-flex; align-items: center; gap: 10px; margin-bottom: 36px; color: var(--text); font-size: .9rem; font-weight: 650; letter-spacing: -.02em; }}
    .brand img {{ width: 30px; height: 30px; object-fit: contain; }}
    .status {{ display: grid; place-items: center; width: 54px; height: 54px; margin: 0 auto 20px; border: 1px solid color-mix(in srgb, var(--success) 30%, var(--border)); border-radius: 18px; background: color-mix(in srgb, var(--success) 10%, transparent); color: var(--success); font-size: 25px; }}
    [data-state="neutral"] .status {{ border-color: var(--border); background: var(--page); color: var(--muted); }}
    h1 {{ margin: 0; font-size: clamp(1.45rem, 5vw, 1.8rem); font-weight: 650; letter-spacing: -.045em; line-height: 1.2; }}
    p {{ margin: 12px 0 28px; color: var(--muted); }}
    a {{ display: inline-flex; min-height: 44px; align-items: center; justify-content: center; padding: 0 18px; border-radius: 11px; background: var(--text); color: var(--page); font-weight: 600; text-decoration: none; transition: opacity .16s ease, transform .16s ease; }}
    a:hover {{ opacity: .88; transform: translateY(-1px); }}
    a:focus-visible {{ outline: 3px solid var(--accent); outline-offset: 3px; }}
    .footnote {{ display: block; margin-top: 22px; color: var(--muted); font-size: .78rem; }}
    @media (prefers-reduced-motion: reduce) {{ *, *::before, *::after {{ scroll-behavior: auto !important; transition-duration: .01ms !important; }} }}
  </style>
</head>
<body>
  <main data-state="{state}">
    <div class="brand"><img src="/static/logo_etu_ai.svg" alt=""><span>ETU-AI</span></div>
    <div class="status" aria-hidden="true">{"✓" if success else "·"}</div>
    <h1>{title}</h1>
    <p>{message}</p>
    <a href="/">Revenir à ETU-AI</a>
    <span class="footnote">Vous pouvez fermer cet onglet une fois de retour dans l’application.</span>
  </main>
</body>
</html>""")


@app.put("/api/settings/provider/chatgpt/select")
def select_chatgpt_account(req: ChatGPTAccountRequest, request: Request):
    require_local_admin(request)
    try:
        chatgpt_auth.select(req.client_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return chatgpt_auth.status()


@app.post("/api/settings/provider/chatgpt/disconnect")
async def disconnect_chatgpt_account(request: Request):
    require_local_admin(request)
    try:
        revoked = await chatgpt_auth.disconnect()
    except Exception:
        logger.exception("ChatGPT disconnect failed")
        raise HTTPException(status_code=503, detail="Impossible de terminer la connexion ChatGPT.")
    state = storage.get_setting("api_providers", {}) or {}
    active_provider = state.get("active_provider")
    active_config = (state.get("configs", {}) or {}).get(active_provider, {})
    active_auth_method = active_config.get("auth_method", "account" if active_provider == "chatgpt" else "api_key")
    if active_provider in {"openai", "chatgpt"} and active_auth_method == "account":
        state["active_provider"] = None
        storage.set_setting("api_providers", state)
    return {"revoked": revoked}


@app.post("/api/settings/provider/models")
async def list_provider_models(req: ProviderModelsRequest, origin: Optional[str] = Header(default=None, alias="Origin")):
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
    if req.provider in {"openai", "chatgpt"} and req.auth_method == "account":
        try:
            token = await chatgpt_auth.access_token()
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                response = await client.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {token}"})
                response.raise_for_status()
                catalog = response.json().get("models", [])
            models = [item["slug"] for item in catalog if isinstance(item, dict) and item.get("visibility") == "list" and isinstance(item.get("slug"), str)]
            if not models:
                raise ValueError("Aucun modèle disponible pour ce compte ChatGPT.")
            efforts = {item["slug"]: levels for item in catalog if isinstance(item, dict) and isinstance(item.get("slug"), str) and (levels := _model_reasoning_efforts(item))}
            return {"models": list(dict.fromkeys(models)), "reasoning_efforts": efforts}
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=502, detail=str(exc) if isinstance(exc, ValueError) else "Impossible de charger les modèles ChatGPT.") from exc
    state = storage.get_setting("api_providers", {}) or {}
    saved = (state.get("configs", {}) or {}).get(req.provider, {})
    base_url = (req.base_url or saved.get("base_url") or preset["base_url"]).strip().rstrip("/")
    try:
        parsed = urlsplit(base_url)
        valid_url = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        valid_url = False
        parsed = None
    if not valid_url or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(status_code=400, detail="Saisissez une URL HTTP ou HTTPS valide, sans identifiants, paramètres ni fragment.")
    api_key = (req.api_key or "").strip()
    if not api_key:
        try:
            api_key = get_api_key(req.provider) or ""
        except Exception:
            api_key = ""
    headers = {"Accept": "application/json", "User-Agent": "ETU-AI/1.0"}
    endpoint = ""
    if preset["type"] == "anthropic":
        endpoint = f"{base_url}/v1/models?limit=1000"
        headers["anthropic-version"] = "2023-06-01"
        if api_key:
            headers["x-api-key"] = api_key
    elif preset["type"] == "gemini":
        endpoint = f"{base_url}/v1beta/models?{urlencode({'pageSize': 1000})}"
        if api_key:
            headers["x-goog-api-key"] = api_key
    else:
        endpoint = f"{base_url}/models"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    try:
        def fetch_models():
            request = urllib.request.Request(endpoint, headers=headers, method="GET")
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read(2_000_000).decode("utf-8"))
        payload = await asyncio.to_thread(fetch_models)
    except Exception as exc:
        status = getattr(exc, "code", None)
        if status == 401:
            raise HTTPException(status_code=502, detail=f"{preset['label']} a rejeté cette clé (401). Vérifiez qu’il s’agit d’une clé API active, copiée sans préfixe « Bearer ».") from exc
        if status == 403:
            raise HTTPException(status_code=502, detail=f"{preset['label']} refuse l’accès (403). Vérifiez les autorisations de la clé et l’accès API du compte.") from exc
        if status:
            raise HTTPException(status_code=502, detail=f"Le fournisseur a répondu avec le statut {status} lors du chargement des modèles.") from exc
        raise HTTPException(status_code=502, detail="Impossible de joindre le fournisseur ou de lire sa réponse de modèles.") from exc
    raw_models = payload.get("data", []) if isinstance(payload, dict) else []
    if preset["type"] == "gemini":
        raw_models = [item for item in payload.get("models", []) if "generateContent" in item.get("supportedGenerationMethods", [])]
    models = []
    reasoning_efforts = {}
    for item in raw_models:
        model_id = item.get("id") or item.get("name") if isinstance(item, dict) else None
        capabilities = item.get("capabilities") if isinstance(item, dict) else None
        if isinstance(capabilities, dict) and capabilities.get("completion_chat") is False:
            continue
        if isinstance(model_id, str):
            model_id = model_id.removeprefix("models/").strip()
            if model_id and len(model_id) <= 200:
                models.append(model_id)
                levels = _model_reasoning_efforts(item)
                if levels:
                    reasoning_efforts[model_id] = levels
    models = sorted(set(models), key=str.casefold)
    if not models:
        raise HTTPException(status_code=502, detail="La réponse du fournisseur ne contient aucun modèle utilisable.")
    return {"models": models, "reasoning_efforts": reasoning_efforts}


@app.get("/api/models/reasoning-capabilities")
async def get_reasoning_capabilities(model_id: str, origin: Optional[str] = Header(default=None, alias="Origin")):
    model = next((item for item in get_configured_api_models() if item["id"] == model_id), None)
    if not model:
        raise HTTPException(status_code=404, detail="Modèle non configuré.")
    if model.get("reasoning_efforts"):
        return {"model_id": model_id, "reasoning_efforts": model["reasoning_efforts"]}
    state = storage.get_setting("api_providers", {}) or {}
    config = state.get("configs", {}).get(model["provider"], {})
    auth_method = config.get("auth_method", "account" if config.get("type") == "chatgpt" else "api_key")
    catalog = await list_provider_models(ProviderModelsRequest(provider=model["provider"], base_url=config.get("base_url"), auth_method=auth_method), origin)
    efforts = catalog.get("reasoning_efforts", {}).get(model["model"], [])
    if efforts:
        if not isinstance(config.get("reasoning_efforts"), dict):
            config["reasoning_efforts"] = {}
        config.setdefault("reasoning_efforts", {})[model["model"]] = efforts
        storage.set_setting("api_providers", state)
    return {"model_id": model_id, "reasoning_efforts": efforts}


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
    model = (req.model or "").strip() or preset["model"]
    try:
        parsed_url = urlsplit(base_url)
        secure_https = parsed_url.scheme in {"http", "https"} and bool(parsed_url.hostname)
    except ValueError:
        secure_https = False
        parsed_url = None
    if not secure_https or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
        raise HTTPException(status_code=400, detail="Saisissez une URL HTTP ou HTTPS valide, sans identifiants, paramètres ni fragment.")
    if len(model) > 200:
        raise HTTPException(status_code=400, detail="Nom de modèle invalide.")
    key = (req.api_key or "").strip()
    if req.provider in {"openai", "chatgpt"} and req.auth_method == "account" and (key or req.clear_api_key):
        raise HTTPException(status_code=400, detail="Une clé API ne peut pas être enregistrée en mode compte ChatGPT.")
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
    if req.provider in {"openai", "chatgpt"}:
        has_key = bool(chatgpt_auth.status()["active"]) if req.auth_method == "account" else bool(get_api_key(req.provider))
    if req.enabled and req.provider in {"openai", "chatgpt"} and req.auth_method == "account" and not has_key:
        raise HTTPException(status_code=400, detail="Connectez le compte ChatGPT avant d’activer le fournisseur.")
    if req.provider == "chatgpt" and req.enabled and not model:
        raise HTTPException(status_code=400, detail="Sélectionnez un modèle ChatGPT avant d'activer le fournisseur.")
    state = storage.get_setting("api_providers", {}) or {}
    configs = state.get("configs", {})
    legacy = storage.get_setting("api_provider", {}) or {}
    if not configs and legacy:
        configs[legacy.get("provider", "openai-compatible")] = legacy
    selected_models = list(dict.fromkeys(name.strip() for name in req.models if isinstance(name, str) and name.strip() and len(name.strip()) <= 200))
    if not selected_models and model:
        selected_models = [model]
    if len(selected_models) > 500:
        raise HTTPException(status_code=400, detail="Sélectionnez au maximum 500 modèles.")
    if selected_models and model not in selected_models:
        model = selected_models[0]
    consolidated_openai = req.provider == "openai"
    provider_type = "chatgpt" if consolidated_openai and req.auth_method == "account" else ("openai" if req.provider == "chatgpt" or consolidated_openai else preset["type"])
    previous_efforts = configs.get(req.provider, {}).get("reasoning_efforts", {})
    supplied_efforts = req.reasoning_efforts or previous_efforts
    if not isinstance(supplied_efforts, dict):
        supplied_efforts = {}
    clean_efforts = {name: levels for name in selected_models if (levels := _clean_reasoning_efforts(supplied_efforts.get(name))) }
    configs[req.provider] = {"base_url": base_url, "model": model, "models": selected_models, "type": provider_type, "reasoning_efforts": clean_efforts, **({"auth_method": req.auth_method} if consolidated_openai or req.provider == "chatgpt" else {})}
    active_provider = req.provider if req.enabled else (None if state.get("active_provider", legacy.get("provider")) == req.provider else state.get("active_provider"))
    storage.set_setting("api_providers", {"active_provider": active_provider, "configs": configs})
    return get_provider_settings(req.provider)


@app.get("/api/setup/state")
def get_app_state():
    """Retourne l'état complet de l'application (ce qui est installé, config, etc.)."""
    return state_manager.get_state()


@app.get("/api/maintenance/server")
def get_server_maintenance(request: Request, refresh: bool = False):
    require_local_admin(request)
    config = load_config()
    state = read_json(STATE_FILE)
    installed_variant = next((part.removeprefix("bin-") for part in Path(state.get("llama_exe_path", "")).parts if part.startswith("bin-")), None)
    with _server_reinstall_lock:
        progress = _server_reinstall.copy()
    try:
        variants = release_variants(config, refresh=refresh)
        catalog_error = None if any(item["compatible"] for item in variants) else "Aucun exécutable compatible dans les releases GitHub"
    except (RuntimeError, ValueError) as exc:
        variants, catalog_error = [], str(exc)
    return {
        "variants": [{"id": item["id"], "label": item["label"], "platform": item["platform"],
                      "arch": item["arch"], "tag": item["tag"], "variant": item["variant"],
                      "compatible": item["compatible"]} for item in variants],
        "selected_variant": next((item["id"] for item in variants if item["compatible"] and item["tag"] == config["llama_release_tag"] and item["variant"] == installed_variant), None),
        "installed_variant": installed_variant,
        "release_tag": config["llama_release_tag"],
        "catalog_error": catalog_error,
        **progress,
    }


@app.get("/api/maintenance/server/progress")
def get_server_reinstall_progress(request: Request):
    require_local_admin(request)
    with _server_reinstall_lock:
        return _server_reinstall.copy()


class ServerReinstallRequest(BaseModel):
    variant: str


def _reinstall_server_variant(asset: dict[str, str]) -> None:
    failure = None
    variant = asset["variant"]
    try:
        with _server_reinstall_lock:
            _server_reinstall.update(status="running", message="En attente de la fin des réponses en cours…")
        with _inference_condition:
            while _active_inference_requests:
                _inference_condition.wait(timeout=1)

        with _server_reinstall_lock:
            _server_reinstall.update(status="running", message="Arrêt du moteur d’inférence…")
        if wizard_manager.is_beacon_online():
            wizard_manager.stop_beacon_listener()
        process = wizard_manager.beacon_process
        if process and process.poll() is None:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        with _server_reinstall_lock:
            _server_reinstall.update(status="running", phase="preparing", message="Préparation du téléchargement…", downloaded_bytes=0, total_bytes=0, speed_mbps=0.0, percentage=0.0)

        def report_download_progress(downloaded: int, total: int, speed_mbps: float, phase: str) -> None:
            with _server_reinstall_lock:
                _server_reinstall.update(
                    status="running", phase=phase,
                    message="Téléchargement de l’archive du moteur…" if phase == "downloading" else "Vérification et installation de l’archive…",
                    downloaded_bytes=downloaded, total_bytes=total,
                    percentage=round(min(100.0, downloaded / total * 100), 1) if total else 0.0,
                    speed_mbps=round(speed_mbps, 2),
                )
        config = load_config()
        backend = backend_for_variant(variant)
        config["backend"] = backend
        config["device"] = "auto"
        config["llama_release_tag"] = asset["tag"]
        config["_exact_asset_name"] = asset["name"]
        config["_exact_asset_url"] = asset["url"]
        if asset["sha256"]:
            config["binary_sha256"] = {**config["binary_sha256"], asset["name"]: asset["sha256"]}
        with file_lock(AI_SERVER_DIR / "setup.lock"), file_lock((BASE_DIR / config["deps_dir"]) / ".maia.lock"):
            setup_llama(scan_hardware(), config=config, exact_variant=variant, progress_callback=report_download_progress)
            saved = read_json(CONFIG_FILE)
            saved["backend"] = backend
            saved["device"] = "auto"
            saved["llama_release_tag"] = asset["tag"]
            if asset["sha256"]:
                saved["binary_sha256"] = {**saved.get("binary_sha256", {}), asset["name"]: asset["sha256"]}
            atomic_json(CONFIG_FILE, saved)
    except Exception as exc:
        logger.exception("Server variant reinstall failed")
        failure = str(exc)
    finally:
        if wizard_manager.is_completed() and wizard_manager.is_beacon_enabled():
            with _server_reinstall_lock:
                _server_reinstall.update(status="running", phase="restarting", message="Redémarrage du moteur d’inférence…")
            if not wizard_manager.ensure_beacon_running() and failure is None:
                failure = "Variante installée, mais le moteur local n’a pas pu redémarrer"
            elif failure is None:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline and not wizard_manager.is_beacon_online():
                    time.sleep(0.5)
                if not wizard_manager.is_beacon_online():
                    failure = "Variante installée, mais le moteur local ne répond pas après son redémarrage"
        global _inference_reinstall_pending
        with _inference_condition:
            _inference_reinstall_pending = False
            _inference_condition.notify_all()
        with _server_reinstall_lock:
            _server_reinstall.update(
                status="error" if failure else "completed",
                message=failure or f"Variante {variant} réinstallée",
            )


@app.post("/api/maintenance/server/reinstall")
def reinstall_server_variant(request: Request, body: ServerReinstallRequest):
    require_local_admin(request)
    if wizard_manager.status == "running":
        raise HTTPException(status_code=409, detail="Attendez la fin de l’installation en cours")
    try:
        asset = next((item for item in release_variants(load_config()) if item["compatible"] and item["id"] == body.variant), None)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if asset is None:
        raise HTTPException(status_code=400, detail="Variante du serveur invalide pour cet ordinateur")
    with _server_reinstall_lock:
        if _server_reinstall["status"] == "running":
            raise HTTPException(status_code=409, detail="Une réinstallation est déjà en cours")
        global _inference_reinstall_pending
        with _inference_condition:
            _inference_reinstall_pending = True
        _server_reinstall.update(
            status="running", message="Préparation de la réinstallation…",
            variant=asset["variant"], release_tag=asset["tag"],
            phase="preparing", downloaded_bytes=0, total_bytes=0, percentage=0.0, speed_mbps=0.0,
        )
    try:
        threading.Thread(target=_reinstall_server_variant, args=(asset,), daemon=True).start()
    except RuntimeError as exc:
        with _inference_condition:
            _inference_reinstall_pending = False
            _inference_condition.notify_all()
        with _server_reinstall_lock:
            _server_reinstall.update(status="error", message="Impossible de démarrer la réinstallation")
        raise HTTPException(status_code=503, detail="Impossible de démarrer la réinstallation") from exc
    return {"started": True}


@app.post("/api/setup/state")
def update_app_state(updates: dict[str, Any]):
    """Permet de mettre à jour des clés de l'état persistant."""
    return state_manager.update_state(updates)


@app.post("/api/setup/start")
def start_setup_endpoint(req: SetupStartRequest):
    asset = None
    backend = req.backend
    if req.selected_variant:
        try:
            variants = release_variants(load_config())
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        asset = next((item for item in variants if item["id"] == req.selected_variant and item["compatible"]), None)
        if asset is None:
            raise HTTPException(status_code=400, detail="Version du moteur indisponible ou incompatible")
        backend = backend_for_variant(asset["variant"])
    started = wizard_manager.start_setup(req.selected_models, backend, selected_asset=asset)
    return {"started": started, "status": wizard_manager.status}


@app.post("/api/setup/cancel")
def cancel_setup_endpoint():
    cancelled = wizard_manager.cancel_setup()
    if not cancelled:
        raise HTTPException(status_code=409, detail="Aucune installation en cours")
    return {"success": True, "status": "cancelling"}


@app.post("/api/setup/defer")
def defer_setup_endpoint():
    """Autorise l'accès à l'application avant la préparation du moteur local."""
    storage.set_setting("setup_deferred", "true")
    return {"success": True}


@app.post("/api/setup/reset")
def reset_setup_endpoint():
    storage.set_setting("setup_completed", "false")
    storage.set_setting("setup_deferred", "false")
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
    loaded_model = active_model

    catalog_items = get_catalog_with_status(hw, installed)
    installed_catalog = [m for m in catalog_items if m.get("is_installed")]
    provider_state = storage.get_setting("api_providers", {}) or {}
    active_provider = provider_state.get("active_provider")
    active_remote_model = next((model for model in get_configured_api_models() if model["provider"] == active_provider), None)
    if saved_selected_model and not active_remote_model:
        active_model = saved_selected_model
        model_ready = model_state == "running" and loaded_model == saved_selected_model
        if model_state == "running" and loaded_model != saved_selected_model:
            model_state = "selected"

    return {
        "app_name": "ETU-AI",
        "status": "online",
        "setup_completed": wizard_manager.is_completed(),
        "beacon_online": beacon_online,
        "active_model": active_model,
        "loaded_model": loaded_model,
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
        "installed_backend": read_json(STATE_FILE).get("backend"),
        "configured_backend": load_config().get("backend"),
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
