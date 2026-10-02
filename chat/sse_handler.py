"""
Gestionnaire de streaming SSE (Server-Sent Events) pour le chat avec Generative UI et Tool Calling.
Intercepte les appels d'outils, émet des fragments HTML en temps réel et persiste la conversation.
"""

from collections.abc import AsyncGenerator
import asyncio
import json
from typing import Any, Optional
import urllib.error
import urllib.request

from chat.prompts import build_system_message
from storage.db import StorageManager
from storage.api_credentials import get_api_key
from tools.executor import ToolExecutor


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so provider credentials cannot be forwarded elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ChatStreamHandler:
    """Orchestre la discussion en flux SSE entre l'UI, le LLM et les outils."""

    def __init__(
        self,
        storage: Optional[StorageManager] = None,
        tool_executor: Optional[ToolExecutor] = None,
        llama_url: str = "http://127.0.0.1:11343",
    ):
        self.storage = storage or StorageManager()
        self.tool_executor = tool_executor or ToolExecutor()
        self.llama_url = llama_url.rstrip("/")

    async def stream_chat(
        self,
        conversation_id: str,
        user_message: str,
        model_name: Optional[str] = None,
        profile: Optional[dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        """Générateur asynchrone émettant des blocs SSE formatés en JSON."""
        # 1. Sauvegarde du message utilisateur dans SQLite
        self.storage.add_message(conversation_id, "user", user_message)

        # 2. Récupération de l'historique récent
        raw_history = self.storage.get_messages(conversation_id)
        system_message = build_system_message()
        if profile:
            name = profile.get("name", "ETU-AI")
            instructions = profile.get("instructions", "")
            system_message["content"] += f"\n\nNom de cet assistant : {name}."
            if instructions:
                system_message["content"] += f"\nPréférences personnelles de l'utilisateur : {instructions}"
        # Garder le contexte utile sans ralentir le pré-remplissage sur CPU.
        recent = []
        remaining = 10000
        for msg in reversed(raw_history[-8:]):
            # Une interface coupée au milieu d'un script ne peut pas être corrigée
            # correctement lors du tour suivant. Garder sa source récente entière.
            is_interface = msg["role"] == "assistant" and ("```html" in msg["content"].lower() or "<script" in msg["content"].lower())
            limit = min(remaining, 8000 if is_interface else (1800 if msg["role"] == "user" else 1200))
            if limit <= 0:
                break
            content = msg["content"]
            if len(content) > limit:
                content = "[Interface précédente trop longue pour le contexte courant : recréer un fragment complet si nécessaire.]" if is_interface else content[:limit] + "…"
            recent.append({"role": msg["role"], "content": content})
            remaining -= len(content)
        messages: list[dict[str, Any]] = [system_message, *reversed(recent)]

        # Etu2 publiera un contrat GET découvrable. Les anciens outils de démonstration
        # ne sont pas exposés tant que ce contrat n'est pas disponible.
        tools = []
        provider_state = self.storage.get_setting("api_providers", {}) or {}
        provider_id = provider_state.get("active_provider")
        if "configs" in provider_state:
            if provider_id:
                provider_config = provider_state.get("configs", {}).get(provider_id, {})
                provider = {
                    **provider_config,
                    "provider": provider_id,
                    "enabled": True,
                    "type": provider_config.get("type", "openai"),
                }
            else:
                provider = {}
        elif provider_id:
            provider = {
                **provider_state.get("configs", {}).get(provider_id, {}),
                "provider": provider_id,
                "enabled": True,
                "type": provider_state.get("configs", {}).get(provider_id, {}).get("type", "openai"),
            }
        else:
            provider = self.storage.get_setting("api_provider", {}) or {}
        use_remote = bool(provider.get("enabled"))
        endpoint = self.llama_url + "/v1/chat/completions"
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        target_model = model_name or "default"
        remote_type = provider.get("type", "openai")
        if use_remote:
            try:
                api_key = get_api_key(provider.get("provider", "openai-compatible"))
            except Exception:
                api_key = None
            if not api_key:
                yield self._sse_event("error", {"message": "La clé API est inaccessible dans le coffre-fort système."})
                yield self._sse_event("done", {"status": "error"})
                return
            target_model = provider["model"]
            base_url = provider["base_url"].rstrip("/")
            if remote_type == "anthropic":
                endpoint = base_url + "/v1/messages"
                headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
            elif remote_type == "gemini":
                endpoint = f"{base_url}/v1beta/models/{target_model}:streamGenerateContent?alt=sse"
                headers["x-goog-api-key"] = api_key
            else:
                endpoint = base_url + "/chat/completions"
                headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": target_model,
            "messages": messages,
            "stream": True,
            "tools": tools if tools else None,
            "temperature": 0.4,
        }
        if not use_remote:
            # Le défaut du moteur peut couper une interface au milieu du script.
            payload["max_tokens"] = 4096
        if use_remote and remote_type == "anthropic":
            payload = {
                "model": target_model,
                "max_tokens": 4096,
                "system": system_message["content"],
                "messages": [message for message in messages if message["role"] != "system"],
                "stream": True,
                "temperature": 0.4,
            }
        elif use_remote and remote_type == "gemini":
            payload = {
                "systemInstruction": {"parts": [{"text": system_message["content"]}]},
                "contents": [
                    {"role": "model" if message["role"] == "assistant" else "user", "parts": [{"text": message["content"]}]}
                    for message in messages if message["role"] != "system"
                ],
                "generationConfig": {"temperature": 0.4},
            }

        # 3. Requête vers le serveur d'inférence (MAIA-Beacon / llama-server)
        yield self._sse_event("status", {"state": "thinking", "message": "L'IA réfléchit..."})

        assistant_full_html = ""
        tool_calls_accumulator: dict[int, dict[str, Any]] = {}

        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            # urllib est bloquant : chaque lecture passe hors de la boucle FastAPI.
            opener = urllib.request.build_opener(_NoRedirect()) if use_remote else None
            open_request = opener.open if opener else urllib.request.urlopen
            resp = await asyncio.to_thread(open_request, req, timeout=120)
            try:
                while True:
                    line = await asyncio.to_thread(resp.readline)
                    if not line:
                        break
                    try:
                        decoded = line.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        decoded = line.decode("cp1252", errors="replace").strip()
                    if not decoded.startswith("data: "):
                        continue
                    data_str = decoded[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if use_remote and remote_type == "anthropic":
                        content_piece = chunk.get("delta", {}).get("text", "") if chunk.get("type") == "content_block_delta" else ""
                    elif use_remote and remote_type == "gemini":
                        parts = chunk.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                        content_piece = "".join(part.get("text", "") for part in parts)
                    else:
                        choices = chunk.get("choices", [])
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        content_piece = delta.get("content", "")

                    if content_piece:
                        assistant_full_html += content_piece
                        yield self._sse_event("token", {"html": content_piece})

                    # B. Accumulation d'appels d'outils
                    if not (use_remote and remote_type in {"anthropic", "gemini"}) and "tool_calls" in delta and delta["tool_calls"]:
                        for tc in delta["tool_calls"]:
                            idx = tc.get("index", 0)
                            if idx not in tool_calls_accumulator:
                                tool_calls_accumulator[idx] = {
                                    "id": tc.get("id", f"call_{idx}"),
                                    "name": tc.get("function", {}).get("name", ""),
                                    "arguments": tc.get("function", {}).get("arguments", ""),
                                }
                            else:
                                if "function" in tc and "arguments" in tc["function"]:
                                    tool_calls_accumulator[idx]["arguments"] += tc["function"]["arguments"]

            finally:
                await asyncio.to_thread(resp.close)

        except (urllib.error.URLError, TimeoutError, ConnectionRefusedError) as e:
            if use_remote:
                message = "Le fournisseur distant est injoignable ou a refusé la requête. Vérifiez l’URL, le modèle et la clé API."
                yield self._sse_event("error", {"message": message})
                yield self._sse_event("done", {"status": "error"})
                self.storage.add_message(conversation_id, "assistant", message)
                return
            # Fallback gracieux si llama-server est éteint ou en cours de chargement
            fallback_html = (
                f'<div class="etu-card">'
                f'<div class="etu-card-header">'
                f'<span class="etu-badge etu-badge-warning">Serveur en attente</span>'
                f'<h4>Connexion au moteur d\'inférence</h4>'
                f'</div>'
                f'<div class="etu-card-body">'
                f'<p class="etu-text">Le modèle d\'IA local est en cours d\'initialisation ou non démarré ({e}).</p>'
                f'<p class="etu-text">Assurez-vous que <code>beacon.py</code> est actif sur le port 11343.</p>'
                f'</div>'
                f'</div>'
            )
            assistant_full_html = fallback_html
            yield self._sse_event("token", {"html": fallback_html})
            yield self._sse_event("done", {"status": "offline_fallback"})
            self.storage.add_message(conversation_id, "assistant", assistant_full_html)
            return

        # 4. Traitement des Tool Calls accumulés si présents
        if tool_calls_accumulator:
            for _, tc_data in tool_calls_accumulator.items():
                tool_name = tc_data["name"]
                args_str = tc_data["arguments"]
                try:
                    args = json.loads(args_str) if args_str else {}
                except json.JSONDecodeError:
                    args = {}

                # Émission de l'indicateur visuel d'attente
                loading_badge = (
                    f'<div class="etu-loading-badge">'
                    f'<span class="etu-spinner"></span> '
                    f'Appel de l\'outil <strong>{tool_name}</strong> sur etu-aix...'
                    f'</div>'
                )
                yield self._sse_event("tool_start", {"tool": tool_name, "html": loading_badge})

                # Exécution de l'outil
                tool_res = self.tool_executor.execute_tool(tool_name, args)
                yield self._sse_event("tool_result", {"tool": tool_name, "result": tool_res})

                # Demande de synthèse finale au modèle avec le résultat d'outil
                synth_messages = messages + [
                    {"role": "assistant", "content": None, "tool_calls": [{"id": tc_data["id"], "type": "function", "function": {"name": tool_name, "arguments": args_str}}]},
                    {"role": "tool", "tool_call_id": tc_data["id"], "content": json.dumps(tool_res)},
                ]
                synth_payload = {
                    "model": model_name or "default",
                    "messages": synth_messages,
                    "stream": True,
                }
                try:
                    synth_req = urllib.request.Request(
                        f"{self.llama_url}/v1/chat/completions",
                        data=json.dumps(synth_payload).encode("utf-8"),
                        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
                        method="POST",
                    )
                    resp = await asyncio.to_thread(urllib.request.urlopen, synth_req, timeout=90)
                    try:
                        while True:
                            line = await asyncio.to_thread(resp.readline)
                            if not line:
                                break
                            try:
                                dec = line.decode("utf-8").strip()
                            except UnicodeDecodeError:
                                dec = line.decode("cp1252", errors="replace").strip()
                            if dec.startswith("data: ") and dec[6:] != "[DONE]":
                                try:
                                    sc = json.loads(dec[6:])
                                    text_delta = sc.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                    if text_delta:
                                        assistant_full_html += text_delta
                                        yield self._sse_event("token", {"html": text_delta})
                                except Exception:
                                    pass
                    finally:
                        await asyncio.to_thread(resp.close)
                except Exception as synth_err:
                    err_html = f'<p class="etu-text etu-badge-warning">Erreur lors de la mise en forme : {synth_err}</p>'
                    assistant_full_html += err_html
                    yield self._sse_event("token", {"html": err_html})

        # 5. Persistance du message assistant complet généré
        self.storage.add_message(conversation_id, "assistant", assistant_full_html)
        yield self._sse_event("done", {"status": "completed"})

    def _sse_event(self, event_type: str, data: dict[str, Any]) -> str:
        """Formate une ligne SSE standard."""
        payload = json.dumps({"event": event_type, **data}, ensure_ascii=False)
        return f"event: {event_type}\ndata: {payload}\n\n"
