"""Stateless Workspace completions using ETU-AI's selected local/cloud model.

No conversation, message, attachment or request is written to storage.
Only existing model settings and the credential vault are read.
"""
import base64
import binascii
import json
import re
from urllib.parse import quote

from fastapi import HTTPException
import httpx
from storage.api_credentials import get_api_key


def selected_model(storage):
    state = storage.get_setting("api_providers", {}) or {}
    provider_id = state.get("active_provider")
    if "configs" in state:
        config = state.get("configs", {}).get(provider_id) if provider_id else None
        if provider_id and not config:
            raise HTTPException(503, "Le modèle sélectionné n'est pas configuré")
    else:
        config = storage.get_setting("api_provider", {}) or {}
        provider_id = config.get("provider", "openai-compatible") if config.get("enabled") else None
    if provider_id:
        if not config.get("model") or not config.get("base_url") or config.get("type", "openai") not in {"openai", "anthropic", "gemini"}:
            raise HTTPException(503, "Configuration du modèle sélectionné invalide")
        return {"model": config["model"], "source": "cloud", "provider": provider_id, "type": config.get("type", "openai"), "base_url": config["base_url"].rstrip("/")}
    return {"model": storage.get_setting("selected_model") or "local", "source": "local", "provider": "", "type": "openai", "base_url": "http://127.0.0.1:11343/v1"}


def selected_profile_public(storage):
    """Only the active assistant's display identity crosses the Workspace bridge."""
    lookup = getattr(storage, "get_profile", None)
    profile = lookup(storage.get_setting("active_profile", "default")) if lookup else None
    profile = profile if isinstance(profile, dict) else {}
    name = profile.get("name")
    name = name.strip()[:50] if isinstance(name, str) and name.strip() else "ETU-AI"
    avatar = profile.get("avatar") or "✦"
    if not isinstance(avatar, str):
        avatar = "✦"
    if avatar.startswith("data:"):
        match = re.fullmatch(r"data:image/webp;base64,([A-Za-z0-9+/]+={0,2})", avatar)
        try:
            image = base64.b64decode(match[1], validate=True) if match and len(avatar) <= 95022 else b""
        except (ValueError, binascii.Error):
            image = b""
        if not image or len(image) > 70000 or image[:4] != b"RIFF" or image[8:12] != b"WEBP":
            avatar = "✦"
    elif len(avatar) > 20 or "<" in avatar or ">" in avatar:
        avatar = "✦"
    profile_id = profile.get("id") if isinstance(profile.get("id"), str) else "default"
    model = profile.get("model") if isinstance(profile.get("model"), str) else None
    return {"id": profile_id, "name": name, "avatar": avatar, "model": model}


def metadata(target):
    return {key: target[key] for key in ("model", "source", "provider")}


def validate_images(images):
    """Only inline bytes are accepted; ETU-AI never fetches document URLs."""
    if not isinstance(images, list) or len(images) > 24:
        raise HTTPException(413, {"code": "workspace_images_invalid"})
    clean, total = [], 0
    for item in images:
        if not isinstance(item, dict) or type(item.get("block")) is not int or item["block"] < 1:
            raise HTTPException(422, {"code": "workspace_images_invalid"})
        url = item.get("data_url")
        if not isinstance(url, str) or len(url) > 12 * 1024 * 1024:
            raise HTTPException(413, {"code": "workspace_images_invalid"})
        match = re.fullmatch(r"data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/]+={0,2})", url)
        if not match:
            raise HTTPException(422, {"code": "workspace_images_invalid"})
        try:
            content = base64.b64decode(match[2], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(422, {"code": "workspace_images_invalid"}) from exc
        kind = match[1]
        valid = (kind == "png" and content.startswith(b"\x89PNG\r\n\x1a\n") or
                 kind == "jpeg" and content.startswith(b"\xff\xd8\xff") or
                 kind == "gif" and content[:6] in (b"GIF87a", b"GIF89a") or
                 kind == "webp" and content.startswith(b"RIFF") and content[8:12] == b"WEBP")
        total += len(content)
        if not valid or len(content) > 8 * 1024 * 1024 or total > 18 * 1024 * 1024:
            raise HTTPException(413, {"code": "workspace_images_invalid"})
        clean.append({"block": item["block"], "data_url": url})
    return clean


async def require_local_vision(target, images):
    if not images or target["source"] != "local":
        return
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            response = await client.get("http://127.0.0.1:11343/api/status")
            response.raise_for_status()
            status = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, {"code": "workspace_local_unavailable"}) from exc
    projector = status.get("active_mmproj")
    if not projector or projector == "__etu_no_mmproj__.gguf":
        raise HTTPException(409, {"code": "workspace_mmproj_missing"})
    model_name = lambda name: str(name or "").replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".gguf")
    if status.get("status") != "running" or model_name(status.get("active_model")) != model_name(target["model"]):
        raise HTTPException(503, {"code": "workspace_local_unavailable"})


def attach_images(payload, images):
    if not images:
        return
    message = next(message for message in reversed(payload["messages"]) if message["role"] == "user")
    content = []
    for image in images:
        content.extend([{"type": "text", "text": f"Image du bloc n° {image['block']} du document :"},
                        {"type": "image_url", "image_url": {"url": image["data_url"]}}])
    # Keep the requested transformation and selected block after the visual context.
    content.append({"type": "text", "text": message["content"]})
    message["content"] = content


def provider_content(content, kind):
    parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
    result = []
    for part in parts:
        if part["type"] == "text":
            result.append({"text": part["text"]} if kind == "gemini" else part)
        else:
            header, data = part["image_url"]["url"].split(",", 1)
            mime = header[5:].split(";", 1)[0]
            result.append({"inlineData": {"mimeType": mime, "data": data}} if kind == "gemini" else
                          {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
    return result


def upstream(target, payload):
    """URLs and keys come exclusively from ETU-AI's saved configuration."""
    headers = {"Content-Type": "application/json"}
    body = {**payload, "model": target["model"]}
    if target["source"] == "local":
        return target["base_url"] + "/chat/completions", headers, body
    try:
        key = get_api_key(target["provider"])
    except Exception as exc:
        raise HTTPException(503, "Clé du modèle sélectionné inaccessible") from exc
    if not key:
        raise HTTPException(503, "Clé du modèle sélectionné manquante")
    system = "\n\n".join(message["content"] for message in body["messages"] if message["role"] == "system")
    messages = [message for message in body["messages"] if message["role"] != "system"]
    if target["type"] == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        messages = [{**message, "content": provider_content(message["content"], "anthropic")} for message in messages]
        body = {"model": target["model"], "system": system, "messages": messages, "stream": body["stream"], "max_tokens": body["max_tokens"], "temperature": body["temperature"]}
        return target["base_url"] + "/v1/messages", headers, body
    if target["type"] == "gemini":
        headers["x-goog-api-key"] = key
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "model" if message["role"] == "assistant" else "user", "parts": provider_content(message["content"], "gemini")} for message in messages], "generationConfig": {"temperature": body["temperature"], "maxOutputTokens": body["max_tokens"]}}
        method = "streamGenerateContent?alt=sse" if payload["stream"] else "generateContent"
        return target["base_url"] + "/v1beta/models/" + quote(target["model"], safe="") + ":" + method, headers, body
    headers["Authorization"] = "Bearer " + key
    # Match the main ETU-AI chat's compatible-provider payload. In particular,
    # newer OpenAI models reject the legacy max_tokens parameter.
    if target["provider"] in {"openai", "openai-compatible"}:
        body.pop("max_tokens", None)
        body["store"] = False
    return target["base_url"] + "/chat/completions", headers, body


def normalized(value, target, streaming=False):
    if value.get("error"):
        raise HTTPException(503, "La génération du modèle sélectionné a échoué")
    actual = value.get("model") or value.get("message", {}).get("model") or value.get("modelVersion")
    if isinstance(actual, str) and actual.strip():
        # Providers may resolve a selected alias to a specific model revision.
        # Local engines may report a filesystem path: keep only the model name.
        target["model"] = (actual.replace("\\", "/").rsplit("/", 1)[-1] if target["source"] == "local" else actual)[:200]
    if target["type"] == "openai":
        result = dict(value)
        result.update({"model": target["model"], "etu_ai": metadata(target)})
        return result
    content, reason = "", None
    if target["type"] == "anthropic":
        if streaming:
            kind = value.get("type")
            if kind == "content_block_delta":
                delta = value.get("delta", {})
                content = delta.get("text", "") if delta.get("type") == "text_delta" else ""
            elif kind == "message_delta":
                stop = value.get("delta", {}).get("stop_reason")
                reason = "length" if stop == "max_tokens" else "stop" if stop else None
            elif kind == "message_stop":
                return "[DONE]"
        else:
            content = "".join(item.get("text", "") for item in value.get("content", []) if item.get("type") == "text")
            reason = "length" if value.get("stop_reason") == "max_tokens" else "stop"
    else:
        candidate = (value.get("candidates") or [{}])[0]
        content = "".join(part.get("text", "") for part in candidate.get("content", {}).get("parts", []) if not part.get("thought"))
        finish = candidate.get("finishReason")
        if finish and finish not in {"STOP", "MAX_TOKENS"}:
            raise HTTPException(503, "Le modèle sélectionné n'a pas terminé la réponse")
        reason = "length" if finish == "MAX_TOKENS" else "stop" if finish else None
    choice = {"delta" if streaming else "message": {"content": content}, "finish_reason": reason, "index": 0}
    return {"model": target["model"], "etu_ai": metadata(target), "choices": [choice]}


def stream_event(line, target):
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return "data: [DONE]\n\n"
    try:
        value = normalized(json.loads(data), target, True)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(503, "Flux du modèle sélectionné invalide") from exc
    return "data: " + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)) + "\n\n"


def final_stream(value, target):
    result = normalized(value, target)
    choice = (result.get("choices") or [{}])[0]
    result["choices"] = [{"delta": {"content": choice.get("message", {}).get("content", "")}, "finish_reason": choice.get("finish_reason"), "index": 0}]
    return "data: " + json.dumps(result, ensure_ascii=False) + "\n\ndata: [DONE]\n\n"
