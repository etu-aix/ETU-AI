"""Explicit profile publication and import; model downloads stay inside ETU-AI."""
import json
import re
from uuid import uuid5, NAMESPACE_URL

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from integration.api import admin, auth
from integration.remote import remote
from models_mgr.downloader import ModelDownloader

router = APIRouter()


class ModelReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["gguf", "local", "api"] = "gguf"
    repo: str | None = Field(default=None, min_length=3, max_length=200)
    filename: str | None = Field(default=None, min_length=1, max_length=250)
    provider: str | None = Field(default=None, min_length=1, max_length=80)
    model_id: str | None = Field(default=None, min_length=1, max_length=250)


class Publication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=4000)
    model: ModelReference | None = None
    accept_workshop_terms: bool = False


class ImportProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: str = "etu-ai-profile"
    version: int = 1
    workshop_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=50)
    avatar: str = Field(default="✦", max_length=95022)
    description: str = Field(max_length=4000)
    instructions: str = Field(default="", max_length=3000)
    model: ModelReference | None = None


def validate_model(model):
    if model:
        if model.type == "api":
            if not model.provider or not model.model_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", model.provider) or not re.fullmatch(r"[^\x00-\x1f\x7f]{1,250}", model.model_id):
                raise HTTPException(422, "Référence du modèle API invalide")
        elif model.type == "local":
            if not model.filename or not re.fullmatch(r"[A-Za-z0-9_.-]+\.gguf", model.filename, re.IGNORECASE):
                raise HTTPException(422, "Référence du modèle local invalide")
        else:
            if not model.repo or not model.filename:
                raise HTTPException(422, "Dépôt et fichier GGUF requis")
            try: ModelDownloader.validate_reference(model.repo, model.filename)
            except ValueError as error: raise HTTPException(422, str(error))


@router.post("/api/profiles/{profile_id}/workshop")
async def publish_profile(profile_id: str, body: Publication, request: Request):
    admin(request)
    from api_server import storage
    profile = storage.get_profile(profile_id)
    if not profile: raise HTTPException(404, "Profil introuvable")
    if not body.accept_workshop_terms or not body.description.strip():
        raise HTTPException(422, "Description et acceptation des conditions requises.")
    validate_model(body.model)
    # Only these portable fields cross the pairing boundary. No provider keys or history.
    payload = {key: profile[key] for key in ("name", "avatar", "instructions")}
    payload.update(source_id=profile_id, description=body.description.strip(),
                   model=body.model.model_dump() if body.model else None, accept_workshop_terms=True)
    result = await remote.publish_profile(payload)
    storage.set_setting("workshop.publication." + profile_id, body.model_dump())
    return result


@router.post("/v1/workshop/import")
def import_profile(body: ImportProfile, request: Request):
    session = auth(request)
    if "etu.workshop.profiles" not in session["permissions"]:
        raise HTTPException(403, "Permission Workshop requise")
    if body.format != "etu-ai-profile" or body.version != 1:
        raise HTTPException(422, "Format de profil incompatible")
    from api_server import storage, downloader, ProfileRequest, validate_profile
    validate_model(body.model)
    name, avatar, instructions = validate_profile(ProfileRequest(name=body.name, avatar=body.avatar, instructions=body.instructions))
    # The paired account identifies an import namespace, avoiding collisions between accounts.
    profile_id = str(uuid5(NAMESPACE_URL, session["pair_id"] + ":workshop:" + str(body.workshop_id)))
    task_id = None
    selected_model = None
    if body.model and body.model.type == "gguf":
        if body.model.filename not in downloader.list_installed_models():
            task_id = downloader.start_download(body.model.repo, body.model.filename)
        selected_model = body.model.filename
    elif body.model and body.model.type == "local":
        if body.model.filename in downloader.list_installed_models():
            selected_model = body.model.filename
    elif body.model and body.model.type == "api":
        from api_server import get_configured_api_models
        configured = get_configured_api_models()
        match = next((item for item in configured if item.get("provider") == body.model.provider and item.get("model") == body.model.model_id), None)
        if match:
            selected_model = match.get("id")
    profile = storage.save_profile(name, avatar, instructions, selected_model, profile_id)
    storage.set_setting("workshop.publication." + profile_id, {"description": body.description, "model": body.model.model_dump() if body.model else None})
    return {"profile": profile, "download_task_id": task_id}


@router.get("/api/profiles/{profile_id}/workshop")
def publication_details(profile_id: str, request: Request):
    admin(request)
    from api_server import storage
    value = storage.get_setting("workshop.publication." + profile_id, {})
    if isinstance(value, str):
        try: value = json.loads(value)
        except ValueError: value = {}
    return value if isinstance(value, dict) else {}
