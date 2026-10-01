"""
Serveur API FastAPI principal pour ETU-AI.
Expose les endpoints de chat SSE, de gestion des modèles Hugging Face, de persistance SQLite,
du handshake avec etu-aix.alwaysdata.net et sert l'interface web statique.
"""

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from typing import Any, Optional
import urllib.request

from fastapi import FastAPI, HTTPException, Request
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
from configs import load_config
from install import scan_hardware
from models_mgr.catalog import get_catalog_with_status
from models_mgr.downloader import ModelDownloader
from storage.db import StorageManager
from tools.executor import ToolExecutor

UI_DIR = BASE_DIR / "ui"

storage = StorageManager()
tool_executor = ToolExecutor()
downloader = ModelDownloader(storage=storage)
stream_handler = ChatStreamHandler(storage=storage, tool_executor=tool_executor)

app = FastAPI(title="ETU-AI Core Server", version="1.0.0")

# Configuration CORS pour etu-aix.alwaysdata.net et localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://etu-aix.alwaysdata.net",
        "http://etu-aix.alwaysdata.net",
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
    installed = downloader.list_installed_models()
    return {
        "catalog": get_catalog_with_status(hw, installed),
        "installed": installed,
        "hardware": {
            "recommended_backend": hw.get("recommended_backend"),
            "gpus": hw.get("gpus"),
            "cpu_cores": hw.get("cpu", {}).get("count"),
        },
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
    try:
        beacon_url = "http://127.0.0.1:11343/api/select"
        payload = json.dumps({"model": req.model, "context_size": req.context_size}).encode("utf-8")
        req_post = urllib.request.Request(
            beacon_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_post, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"success": True, "result": data}
    except Exception as e:
        return {"success": False, "error": f"Impossible de contacter Beacon : {e}"}


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
    return {
        "status": "online",
        "paired_with_etu": bool(token),
        "installed_models_count": len(installed),
        "backend": hw.get("recommended_backend"),
        "gpus": hw.get("gpus"),
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
