"""Outgoing ETU 2 relay connection. Private identity stays in the existing vault."""
import asyncio
import base64
import json
import logging
import os
import random
import re
import secrets
import socket
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from integration.bridge import bridge, digest, ALLOW_LOCALHOST_ORIGINS

logger = logging.getLogger(__name__)

SCOPES = {"ai.chat": None, "group.read": "etu.profile.group", "timetable.read": "etu.schedule.read",
          "workspace.read": "etu.workspace.read", "ai.models.read": "etu.ai.models.read", "ai.models.select": "etu.ai.model.select"}
ROUTES = {("GET", "/v1/status"): [], ("GET", "/v1/identity"): [],
          ("GET", "/v1/profile"): ["workspace.read"], ("GET", "/v1/models"): ["ai.models.read"],
          ("GET", "/v1/models/status"): ["ai.models.read"],
          ("POST", "/v1/models/select"): ["ai.models.read", "ai.models.select"],
          ("POST", "/v1/chat/completions"): ["ai.chat"]}


def canonical(challenge_id, nonce, installation_id, pair_id):
    return "\n".join(["ETU-AI-REMOTE-V1", challenge_id, nonce, installation_id, pair_id]).encode()


def validate_url(url, allow_loopback=False):
    parsed = urlsplit(url)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname
            or parsed.scheme != "wss" and not (allow_loopback and parsed.scheme == "ws" and parsed.hostname in {"127.0.0.1", "localhost", "::1"})):
        raise ValueError("La connexion distante nécessite WSS, sans identifiant dans l’URL.")
    return url


class RemoteClient:
    def __init__(self, identity=bridge, url=None, allow_loopback=False):
        self.identity = identity
        self.url = validate_url(url or os.environ.get("ETU_AI_REMOTE_WS_URL", "wss://etu-aix.alwaysdata.net/wss"), allow_loopback)
        self.app = None
        self.task = None
        self.ws = None
        self.jobs = {}
        self.state = "disabled"
        self.pairing = None
        self.scopes = []
        self.code = None
        self.expires_at = 0
        self.last_error = None
        self.last_seen = 0
        self.device_name = socket.gethostname()[:100]
        self.send_lock = asyncio.Lock()
        self.disconnect_ack = asyncio.Event()
        self.generation = 0
        self.tool_key = None
        self.configuration_valid = True

    def stored(self):
        with self.identity.lock:
            self.identity.identity()
            return self.identity.record.get("remote_pair")

    def save(self, value):
        with self.identity.lock:
            self.identity.identity()
            previous = self.identity.record.get("remote_pair")
            if value is None:
                self.identity.record.pop("remote_pair", None)
            else:
                self.identity.record["remote_pair"] = value
            try:
                self.identity._save()
            except Exception:
                if previous is None: self.identity.record.pop("remote_pair", None)
                else: self.identity.record["remote_pair"] = previous
                raise

    def status(self):
        try:
            pair = self.stored()
        except Exception:
            # Keep the local AI server available if this optional integration's
            # DPAPI identity belongs to another Windows profile or is unreadable.
            pair = None
            self.state = "error"
            self.last_error = "identity_unavailable"
        return {"status": self.state, "pair_id": pair.get("pair_id") if pair else None,
                "scopes": pair.get("scopes", []) if pair else self.scopes,
                "device_name": self.device_name, "pairing_code": self.code if time.time() < self.expires_at else None,
                "expires_at": self.expires_at, "error_code": self.last_error}

    async def start(self, app):
        self.app = app
        if not self.configuration_valid:
            self.state = "error"
            return
        try:
            pair = self.stored()
        except Exception:
            logger.exception("ETU remote integration is unavailable because its local identity cannot be read")
            self.state = "error"
            self.last_error = "identity_unavailable"
            return
        if pair and not self.task:
            self.device_name = pair.get("device_name", self.device_name)
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.generation += 1
        task, self.task = self.task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.ws:
            await self.ws.close()
        self.ws = None
        await self.cancel_jobs()
        self.clear_tools()
        self.code = None
        self.state = "disabled"

    async def disconnect(self):
        if self.stored():
            if not self.ws or self.state != "connected":
                raise HTTPException(503, "Reconnectez ETU 2 avant de déconnecter cet appareil, ou retirez-le depuis ETU 2.")
            self.disconnect_ack.clear()
            await self.send("device.disconnect")
            try:
                await asyncio.wait_for(self.disconnect_ack.wait(), 5)
            except TimeoutError:
                raise HTTPException(504, "La déconnexion n’a pas été confirmée par ETU 2. Réessayez.")
        await self.stop()
        self.save(None)
        self.pairing = None

    async def create_pairing(self, scopes, device_name):
        if not self.configuration_valid:
            raise HTTPException(422, "Configuration distante invalide : une URL WSS sans identifiant est requise.")
        if self.stored():
            raise HTTPException(409, "Déconnectez l’association distante avant un nouvel appairage.")
        if not scopes or len(scopes) != len(set(scopes)) or not set(scopes) <= SCOPES.keys():
            raise HTTPException(422, "Autorisations invalides")
        await self.stop()
        self.scopes = sorted(scopes)
        self.device_name = device_name.strip()[:100] or socket.gethostname()[:100]
        self.pairing = True
        self.expires_at = time.time() + 300
        self.state = "connecting"
        self.task = asyncio.create_task(self.run())
        return self.status()

    async def send(self, kind, **values):
        async with self.send_lock:
            if self.ws:
                await self.ws.send(json.dumps({"v": 1, "type": kind, "timestamp": int(time.time()), **values}, ensure_ascii=False, separators=(",", ":")))

    async def cancel_jobs(self):
        jobs = list(self.jobs.values())
        for job in jobs: job.cancel()
        if jobs: await asyncio.gather(*jobs, return_exceptions=True)
        self.jobs.clear()

    def clear_tools(self):
        if self.tool_key:
            from integration.tool_relay import tool_relay
            tool_relay.unregister(self.tool_key)
            with self.identity.lock:
                self.identity.sessions.pop(self.tool_key, None)
            self.tool_key = None

    async def heartbeat(self):
        while True:
            await asyncio.sleep(25)
            if time.monotonic() - self.last_seen > 75:
                await self.ws.close()
                return
            await self.send("ping")

    async def run(self):
        try:
            from websockets.asyncio.client import connect
        except ImportError:
            self.last_error = "DEPENDENCY_MISSING"
            self.state = "error"
            self.pairing = None
            self.code = None
            return
        retry = 0
        generation = self.generation
        while generation == self.generation and (self.stored() or self.pairing):
            if self.pairing and time.time() >= self.expires_at:
                self.state = "expired"; self.pairing = None; self.code = None; return
            self.state = "connecting" if retry == 0 else "reconnecting"
            try:
                async with connect(self.url, compression=None, ping_interval=25, ping_timeout=75, open_timeout=10,
                                   close_timeout=3, max_size=26 * 1024 * 1024, max_queue=4, logger=_quiet_logger()) as ws:
                    self.ws = ws
                    self.last_seen = time.monotonic()
                    identity = self.identity.identity()
                    pair = self.stored()
                    if pair:
                        await self.send("device.hello", installation_id=identity["instance_id"], pair_id=pair["pair_id"])
                    else:
                        await self.send("pair.create", installation_id=identity["instance_id"], public_key=identity["public_key"], device_name=self.device_name, scopes=self.scopes)
                    heartbeat = asyncio.create_task(self.heartbeat())
                    tools = asyncio.create_task(self.tool_loop())
                    try:
                        async for wire in ws:
                            if not isinstance(wire, str): raise ValueError("INVALID_MESSAGE")
                            message = json.loads(wire)
                            if not isinstance(message, dict) or message.get("v") != 1 or not isinstance(message.get("type"), str): raise ValueError("INVALID_MESSAGE")
                            self.last_seen = time.monotonic()
                            await self.receive(message)
                            if self.state == "connected": retry = 0
                    finally:
                        heartbeat.cancel(); await asyncio.gather(heartbeat, return_exceptions=True)
                        tools.cancel(); await asyncio.gather(tools, return_exceptions=True)
                        self.clear_tools()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Socket internals, payloads and credentials must never enter logs.
                self.last_error = self.last_error or "CONNECTION_LOST"
            finally:
                self.ws = None
                await self.cancel_jobs()
            if generation != self.generation or not (self.stored() or self.pairing): return
            delays = [1, 2, 5, 10, 30]
            await asyncio.sleep(delays[min(retry, len(delays)-1)] + random.uniform(0, .5))
            retry += 1

    async def receive(self, m):
        kind = m["type"]
        if kind == "ping": await self.send("pong")
        elif kind == "pair.created":
            code = m.get("pairing_code")
            if not isinstance(code, str) or not re.fullmatch(r"ETU-[A-Z2-9]{4}-[A-Z2-9]{4}", code): raise ValueError("INVALID_MESSAGE")
            self.code = code; self.state = "pairing"
            self.expires_at = time.time() + min(300, int(m.get("expires_in", 300)))
        elif kind == "pair.approved":
            scopes = m.get("scopes")
            from uuid import UUID
            if UUID(m["pair_id"]).version != 4 or str(UUID(m["pair_id"])) != m["pair_id"]: raise ValueError("INVALID_MESSAGE")
            if not isinstance(scopes, list) or not scopes or not set(scopes) <= set(self.scopes): raise ValueError("INVALID_MESSAGE")
            self.save({"pair_id": m["pair_id"], "scopes": scopes, "device_name": self.device_name})
            self.code = None; self.pairing = None
            await self.send("device.hello", installation_id=self.identity.identity()["instance_id"], pair_id=m["pair_id"])
        elif kind == "device.challenge":
            pair = self.stored()
            if not pair or len(base64.b64decode(m["nonce"], validate=True)) != 32 or len(m["challenge_id"]) != 36: raise ValueError("INVALID_CHALLENGE")
            payload = canonical(m["challenge_id"], m["nonce"], self.identity.identity()["instance_id"], pair["pair_id"])
            signature = base64.b64encode(self.identity.key().sign(payload)).decode()
            await self.send("device.authenticate", challenge_id=m["challenge_id"], signature=signature)
        elif kind == "device.authenticated":
            pair = self.stored()
            if not pair or m.get("pair_id") != pair["pair_id"] or sorted(m.get("scopes", [])) != sorted(pair["scopes"]): raise ValueError("INVALID_MESSAGE")
            self.state = "connected"; self.last_error = None
            from integration.tool_relay import tool_relay
            self.tool_key = digest(secrets.token_urlsafe(32))
            permissions = [SCOPES[s] for s in pair["scopes"] if SCOPES[s]]
            session = {"pair_id": pair["pair_id"], "origin": "https://etu-aix.alwaysdata.net", "permissions": permissions, "expires_at": self.identity.clock() + 130, "context": {}}
            self.identity.sessions[self.tool_key] = session
            catalog = [{"name": name} for name, permission in [("profile.group", "etu.profile.group"), ("schedule.read", "etu.schedule.read")] if permission in permissions]
            if catalog:
                tool_relay.register(self.identity, self.tool_key, session, catalog)
            else:
                self.identity.sessions.pop(self.tool_key, None)
                self.tool_key = None
        elif kind == "tools.result":
            from integration.tool_relay import tool_relay
            if self.tool_key:
                try: tool_relay.result(self.tool_key, m.get("request_id"), m.get("result", {}))
                except HTTPException: pass
        elif kind == "device.revoked" or kind == "error" and m.get("error", {}).get("code") in {"PAIR_REVOKED", "PAIR_NOT_FOUND"}:
            self.save(None); self.pairing = None; self.code = None
            await self.cancel_jobs()
            self.clear_tools()
            self.state = "revoked"
            self.disconnect_ack.set()
            await self.ws.close()
        elif kind in {"pair.rejected", "pair.expired"}:
            self.pairing = None; self.code = None; self.state = "rejected" if kind == "pair.rejected" else "expired"; await self.ws.close()
        elif kind == "error":
            self.last_error = m.get("error", {}).get("code", "INTERNAL_ERROR")
            await self.ws.close()
        elif kind == "ai.cancel":
            job = self.jobs.get(m.get("request_id"))
            if job: job.cancel()
        elif kind == "ai.request":
            request_id = m.get("request_id")
            pair = self.stored()
            if self.state != "connected" or not pair or m.get("pair_id") != pair["pair_id"] or not isinstance(request_id, str) or len(request_id) != 36 or request_id in self.jobs: raise ValueError("INVALID_MESSAGE")
            if len(self.jobs) >= 4:
                await self.send("ai.response.error", request_id=request_id, error={"code": "RATE_LIMITED"}); return
            task = asyncio.create_task(self.execute(request_id, m.get("payload")))
            self.jobs[request_id] = task
            task.add_done_callback(lambda done, key=request_id: self.jobs.pop(key, None))

    async def execute(self, request_id, payload):
        token = secrets.token_urlsafe(32)
        try:
            async with asyncio.timeout(120):
                pair = self.stored()
                if not isinstance(payload, dict) or set(payload) - {"path", "method", "body", "context"}: raise HTTPException(422, "INVALID_MESSAGE")
                key = (payload.get("method", "GET"), payload.get("path"))
                if key not in ROUTES or not set(ROUTES[key]) <= set(pair["scopes"]): raise HTTPException(403, "PERMISSION_DENIED")
                permissions = [SCOPES[s] for s in pair["scopes"] if SCOPES[s]]
                with self.identity.lock:
                    self.identity.sessions[digest(token)] = {"pair_id": pair["pair_id"], "origin": "https://etu-aix.alwaysdata.net", "permissions": permissions, "expires_at": self.identity.clock() + 130, "context": {}}
                await self.send("ai.response.start", request_id=request_id)
                result = await self.dispatch(key, payload, token)
                if isinstance(result, StreamingResponse):
                    content = ""; model = {}; buffer = ""; done = False; reason = None
                    async for chunk in result.body_iterator:
                        buffer += chunk.decode() if isinstance(chunk, bytes) else chunk
                        while "\n\n" in buffer:
                            event, buffer = buffer.split("\n\n", 1)
                            value = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                            if not value: continue
                            if value == "[DONE]": done = True; continue
                            data = json.loads(value)
                            if data.get("error"): raise HTTPException(503, data["error"])
                            if data.get("model"): model = {"model": data["model"], "etu_ai": data.get("etu_ai", {})}
                            choice = (data.get("choices") or [{}])[0]
                            reason = choice.get("finish_reason") or reason
                            delta = choice.get("delta", {}).get("content", "")
                            if delta is None:
                                delta = ""
                            if not isinstance(delta, str): raise HTTPException(502, "INVALID_MESSAGE")
                            content += delta
                            if len(content) > 200000: raise HTTPException(413, "INVALID_MESSAGE")
                            if delta: await self.send("ai.response.delta", request_id=request_id, delta=delta, etu_ai={"model": model.get("model", ""), **model.get("etu_ai", {})})
                    if not done or not content or reason not in {None, "stop"}: raise HTTPException(502, "INCOMPLETE_RESPONSE")
                    result = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], **model}
                await self.send("ai.response.done", request_id=request_id, data=result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = "REQUEST_TIMEOUT" if isinstance(exc, TimeoutError) else "INTERNAL_ERROR"
            status = 503
            if isinstance(exc, HTTPException):
                status = exc.status_code
                detail = exc.detail.get("code") if isinstance(exc.detail, dict) else exc.detail
                code = detail if isinstance(detail, str) and (detail.isupper() or detail.startswith("workspace_")) else "PERMISSION_DENIED" if status == 403 else "INVALID_MESSAGE" if status in {400, 422, 413} else "MODEL_UNAVAILABLE"
            import logging
            logging.getLogger(__name__).warning("Remote request failed: %s (%s, HTTP %s)", code, type(exc).__name__, status)
            try:
                await self.send("ai.response.error", request_id=request_id, error={"code": code, "status": status})
            except Exception:
                pass  # A lost socket is already handled by the connection lifecycle.
        finally:
            with self.identity.lock: self.identity.sessions.pop(digest(token), None)

    async def tool_loop(self):
        from integration.tool_relay import tool_relay
        sent = set()
        while True:
            await asyncio.sleep(.25)
            if not self.tool_key or self.state != "connected": continue
            with self.identity.lock:
                session = self.identity.sessions.get(self.tool_key)
                if session: session["expires_at"] = self.identity.clock() + 130
            batch = tool_relay.poll(self.tool_key)
            ids = {item["request_id"] for item in batch}
            sent.intersection_update(ids)
            for item in batch:
                if item["request_id"] not in sent:
                    sent.add(item["request_id"])
                    await self.send("tools.call", request_id=item["request_id"], payload={"tool": item["tool"], "arguments": item["arguments"]})

    async def dispatch(self, key, payload, token):
        from integration import api as routes
        async def receive():
            await asyncio.sleep(3600)
        request = Request({"type": "http", "method": key[0], "path": key[1], "app": self.app,
                           "headers": [(b"origin", b"https://etu-aix.alwaysdata.net"), (b"authorization", ("Bearer " + token).encode())]}, receive)
        values = payload.get("context", {})
        if values:
            await asyncio.to_thread(routes.context, routes.ContextRequest.model_validate(values), request)
        if key[1] == "/v1/status": return routes.status()
        if key[1] == "/v1/identity": return self.identity.identity()
        if key[1] == "/v1/profile": return await asyncio.to_thread(routes.workspace_profile, request)
        if key[1] == "/v1/models": return await asyncio.to_thread(routes.installed_models, request)
        if key[1] == "/v1/models/status": return await routes.selected_model_status(request)
        if key[1] == "/v1/models/select": return await asyncio.to_thread(routes.select_workspace_model, routes.ModelSelectionRequest.model_validate(payload.get("body")), request)
        return await routes.completions(routes.CompletionRequest.model_validate(payload.get("body")), request)


def _quiet_logger():
    import logging
    logger = logging.getLogger("etu_ai.remote.wire")
    logger.disabled = True
    return logger


try:
    remote = RemoteClient(allow_loopback=ALLOW_LOCALHOST_ORIGINS)
except ValueError:
    # An invalid remote setting must never prevent the local bridge/UI from starting.
    remote = RemoteClient(url="wss://etu-aix.alwaysdata.net/wss")
    remote.configuration_valid = False
    remote.state = "error"
    remote.last_error = "INVALID_CONFIGURATION"
router = APIRouter()


class PairBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # One code associates the complete ETU integration. Scope checks remain enforced on every route.
    scopes: list[str] = Field(default_factory=lambda: sorted(SCOPES), min_length=1, max_length=6)
    device_name: str = Field(default=socket.gethostname()[:100], min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")


@router.get("/api/etu/remote")
async def status(request: Request):
    from integration.api import admin
    admin(request)
    return remote.status()


@router.post("/api/etu/remote/pair")
async def pair(body: PairBody, request: Request):
    from integration.api import admin
    admin(request)
    remote.app = request.app
    return await remote.create_pairing(body.scopes, body.device_name)


@router.delete("/api/etu/remote")
async def disconnect(request: Request):
    from integration.api import admin
    admin(request)
    await remote.disconnect()
    return remote.status()
