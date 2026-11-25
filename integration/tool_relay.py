"""Volatile ETU tool calls relayed through an approved ETU 2 connection."""
import asyncio
import copy
import json
import threading
import time
from uuid import uuid4
from fastapi import HTTPException

TOOLS = {
    "profile.group": ("etu.profile.group", "etu_profile_group", "Lire le groupe du compte ETU connecté.", {}),
    "schedule.read": ("etu.schedule.read", "etu_schedule_read", "Lire les cours et événements personnels sur 1 à 31 jours et la carte des salles/prises. Les événements personnels appartenant à l’utilisateur fournissent event_id, nécessaire pour les partager. Dates YYYY-MM-DD, fin exclusive, fuseau Europe/Paris.", {"from": {"type": "string", "format": "date"}, "to": {"type": "string", "format": "date"}}),
    "outlets.read": ("etu.schedule.read", "etu_outlets_map_read", "Consulter la carte ETU 2 des salles et prises. Utiliser query ou les filtres group, room et id_text pour trouver les salles pertinentes ; has_outlets filtre les salles avec prises.", {"query": {"type": "string", "maxLength": 100}, "group": {"type": "string", "maxLength": 100}, "room": {"type": "string", "maxLength": 100}, "id_text": {"type": "string", "maxLength": 100}, "has_outlets": {"type": "boolean"}}),
    "schedule.create": ("etu.schedule.write", "etu_schedule_create", "Ajouter un événement personnel à l’EDT. Dates/heures ISO 8601 avec fuseau. Mettre confirm à true quand l’utilisateur demande un formulaire modifiable, veut partager l’ajout, demande une validation ou si les détails sont incertains ; le formulaire permet d’ajuster l’événement et de le partager en une action. Quand les détails sont clairs et qu’aucun ajustement ni partage n’est demandé, confirm=false permet l’ajout direct.", {"title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "location": {"type": "string"}, "description": {"type": "string"}, "confirm": {"type": "boolean"}}),
    "friends.list": ("etu.schedule.write", "etu_friends_list", "Lister les amis du compte ETU pour choisir les destinataires d’un événement.", {}),
    "schedule.share": ("etu.schedule.write", "etu_schedule_share", "Partager un événement personnel appartenant à l’utilisateur avec les amis choisis. Prendre event_id dans personal_event_series d’etu_schedule_read et friend_ids dans etu_friends_list. Si l’utilisateur a explicitement demandé le partage et que l’événement et les destinataires sont clairs, partager directement avec confirm=false. Mettre confirm=true seulement si l’utilisateur demande une validation ou si un choix/détail reste incertain.", {"event_id": {"type": "integer"}, "friend_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1, "maxItems": 100}, "confirm": {"type": "boolean"}}),
}

OPTIONAL_ARGUMENTS = {"outlets.read": set(TOOLS["outlets.read"][3])}


class ToolRelay:
    def __init__(self):
        self.lock = threading.RLock()
        self.providers = {}

    def _live(self, provider):
        with provider["bridge"].lock:
            provider["bridge"].prune()
            return provider["key"] in provider["bridge"].sessions and time.monotonic() - provider["seen"] < 30

    def register(self, bridge, key, session, catalog):
        names = [value.get("name") for value in catalog]
        if any(not isinstance(name, str) for name in names) or len(names) != len(set(names)) or any(name not in TOOLS or TOOLS[name][0] not in session["permissions"] for name in names):
            raise HTTPException(403, "Outils non autorisés")
        with self.lock:
            self.providers = {key: value for key, value in self.providers.items() if self._live(value)}
            existing = self.providers.get(key)
            self.providers[key] = {"key": key, "bridge": bridge, "pair_id": session["pair_id"], "origin": session["origin"], "seen": time.monotonic(), "names": names, "pending": existing["pending"] if existing else {}}

    def binding(self):
        with self.lock:
            available = [value for value in self.providers.values() if self._live(value)]
            # Never choose arbitrarily between two different accounts/installations.
            if not available or len({(value["origin"], value["pair_id"]) for value in available}) != 1:
                return None
            return max(available, key=lambda value: value["seen"])["key"]

    def tools(self, key):
        with self.lock:
            provider = self.providers.get(key)
            if not provider or not self._live(provider):
                return []
            result = []
            for name in provider["names"]:
                _, function, description, properties = TOOLS[name]
                properties = copy.deepcopy(properties)
                for key in OPTIONAL_ARGUMENTS.get(name, set()):
                    schema = properties[key]
                    field_type = schema.get("type")
                    if isinstance(field_type, str):
                        schema["type"] = [field_type, "null"]
                    elif isinstance(field_type, list) and "null" not in field_type:
                        schema["type"] = [*field_type, "null"]
                required = list(properties)
                result.append({"type": "function", "function": {"name": function, "description": description, "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}})
            return result

    def poll(self, key):
        with self.lock:
            provider = self.providers.get(key)
            if not provider or not self._live(provider):
                raise HTTPException(409, "Reconnectez les outils ETU")
            provider["seen"] = time.monotonic()
            return [{"request_id": request_id, "tool": value["tool"], "arguments": value["arguments"]} for request_id, value in provider["pending"].items() if not value["future"].done() and not value.get("answered")]

    def result(self, key, request_id, result):
        if len(json.dumps(result).encode()) > 131072 or not isinstance(result.get("success"), bool) or set(result) - {"success", "data", "message"}:
            raise HTTPException(422, "Résultat invalide ou trop volumineux")
        with self.lock:
            provider = self.providers.get(key)
            pending = provider["pending"].get(request_id) if provider and self._live(provider) else None
            if not pending or pending["future"].done() or pending.get("answered"):
                raise HTTPException(404, "Demande absente ou terminée")
            pending["answered"] = True
            def complete():
                if not pending["future"].done():
                    pending["future"].set_result(result)
            pending["loop"].call_soon_threadsafe(complete)

    def unregister(self, key):
        with self.lock:
            self.providers.pop(key, None)

    async def call(self, key, function, arguments):
        name = next((name for name, value in TOOLS.items() if value[1] == function), None)
        with self.lock:
            provider = self.providers.get(key)
            if not provider or not self._live(provider) or name not in provider["names"]:
                return {"success": False, "message": "Outil ETU non disponible ou non autorisé."}
            expected = TOOLS[name][3]
            required = set(expected) - OPTIONAL_ARGUMENTS.get(name, set())
            if not isinstance(arguments, dict) or set(arguments) - set(expected) or required - set(arguments) or len(json.dumps(arguments).encode()) > 4096:
                return {"success": False, "message": "Paramètres ETU invalides."}
            if len(provider["pending"]) >= 20:
                return {"success": False, "message": "Trop de demandes ETU."}
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            request_id = uuid4().hex
            provider["pending"][request_id] = {"tool": name, "arguments": arguments, "future": future, "loop": loop}
        try:
            return await asyncio.wait_for(future, 15)
        except asyncio.TimeoutError:
            return {"success": False, "message": "ETU 2 ne répond pas. Gardez un onglet ouvert et connecté."}
        finally:
            with self.lock:
                provider["pending"].pop(request_id, None)


tool_relay = ToolRelay()
