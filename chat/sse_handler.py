"""
Gestionnaire de streaming SSE (Server-Sent Events) pour le chat avec Generative UI et Tool Calling.
Intercepte les appels d'outils, émet des fragments HTML en temps réel et persiste la conversation.
"""

from collections.abc import AsyncGenerator
import json
from typing import Any, Optional
import urllib.error
import urllib.request

from chat.prompts import build_system_message
from storage.db import StorageManager
from tools.executor import ToolExecutor


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
    ) -> AsyncGenerator[str, None]:
        """Générateur asynchrone émettant des blocs SSE formatés en JSON."""
        # 1. Sauvegarde du message utilisateur dans SQLite
        self.storage.add_message(conversation_id, "user", user_message)

        # 2. Récupération de l'historique récent
        raw_history = self.storage.get_messages(conversation_id)
        messages: list[dict[str, Any]] = [build_system_message()]
        for msg in raw_history[-10:]:
            messages.append({"role": msg["role"], "content": msg["content"]})

        tools = self.tool_executor.get_openai_tools()
        payload = {
            "model": model_name or "default",
            "messages": messages,
            "stream": True,
            "tools": tools if tools else None,
            "temperature": 0.4,
        }

        # 3. Requête vers le serveur d'inférence (MAIA-Beacon / llama-server)
        yield self._sse_event("status", {"state": "thinking", "message": "L'IA réfléchit..."})

        assistant_full_html = ""
        tool_calls_accumulator: dict[int, dict[str, Any]] = {}

        try:
            req = urllib.request.Request(
                f"{self.llama_url}/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
                method="POST",
            )
            # Lecture du flux SSE de llama-server
            with urllib.request.urlopen(req, timeout=120) as resp:
                for line in resp:
                    decoded = line.decode("utf-8", errors="replace").strip()
                    if not decoded.startswith("data: "):
                        continue
                    data_str = decoded[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})

                    # A. Réception de fragments de texte HTML
                    if "content" in delta and delta["content"]:
                        content_piece = delta["content"]
                        assistant_full_html += content_piece
                        yield self._sse_event("token", {"html": content_piece})

                    # B. Accumulation d'appels d'outils
                    if "tool_calls" in delta and delta["tool_calls"]:
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

        except (urllib.error.URLError, TimeoutError, ConnectionRefusedError) as e:
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
                    with urllib.request.urlopen(synth_req, timeout=90) as resp:
                        for line in resp:
                            dec = line.decode("utf-8", errors="replace").strip()
                            if dec.startswith("data: ") and dec[6:] != "[DONE]":
                                try:
                                    sc = json.loads(dec[6:])
                                    text_delta = sc.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                    if text_delta:
                                        assistant_full_html += text_delta
                                        yield self._sse_event("token", {"html": text_delta})
                                except Exception:
                                    pass
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
