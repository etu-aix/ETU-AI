"""Local ETU identity, explicit grants and volatile, origin-bound sessions."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from uuid import uuid4
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from storage.api_credentials import _dpapi

ETU_ORIGINS = {"https://etu-aix.alwaysdata.net"}
ALLOW_LOCALHOST_ORIGINS = (
    os.environ.get("ETU_LAUNCH_MODE", "normal").strip().lower() == "local"
    and os.environ.get("ETU_ALLOW_LOCALHOST_ORIGINS", "0").strip().lower() not in {"0", "false", "no", "off"}
)
LOCALHOST_ORIGIN_REGEX = r"^http://(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?$"
PERMISSIONS = {
    "etu.profile.group",
    "etu.schedule.read",
    "etu.workspace.read",
    "etu.ai.models.read",
    "etu.ai.model.select",
}
SESSION_TTL = 1800
PAIR_TTL = 300


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def is_etu_origin(value):
    if value in ETU_ORIGINS:
        return True
    if not ALLOW_LOCALHOST_ORIGINS or not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and parsed.query == ""
        and parsed.fragment == ""
        and (port is None or 1 <= port <= 65535)
    )


class Bridge:
    def __init__(self, path=None, clock=time.time):
        self.path = Path(path or os.environ.get("ETU_AI_IDENTITY_FILE") or Path(__file__).resolve().parent.parent / "storage" / "etu_identity.dpapi")
        self.clock = clock
        self.lock = threading.RLock()
        self.record = None
        self.pending = {}
        self.challenges = {}
        self.sessions = {}
        self.admin_token = secrets.token_urlsafe(32)

    def _read(self):
        if os.name == "nt":
            return _dpapi(self.path.read_bytes(), decrypt=True) if self.path.exists() else None
        import keyring
        value = keyring.get_password("ETU-AI", "bridge:" + str(self.path.resolve()))
        return value.encode() if value else None

    def _save(self):
        raw = json.dumps(self.record, separators=(",", ":")).encode()
        if os.name != "nt":
            import keyring
            keyring.set_password("ETU-AI", "bridge:" + str(self.path.resolve()), raw.decode())
            return
        encrypted = _dpapi(raw, decrypt=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as handle:
                name = handle.name
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if name:
                Path(name).unlink(missing_ok=True)

    def identity(self):
        with self.lock:
            if self.record is None:
                raw = self._read()
                if raw:
                    self.record = json.loads(raw)
                else:
                    key = Ed25519PrivateKey.generate()
                    self.record = {"instance_id": str(uuid4()), "private_key": base64.b64encode(key.private_bytes_raw()).decode(), "grants": {}}
                    try:
                        self._save()
                    except Exception:
                        self.record = None
                        raise
            key = self.key()
            return {"name": "ETU-AI", "version": "1.0.0", "instance_id": self.record["instance_id"], "algorithm": "Ed25519", "public_key": base64.b64encode(key.public_key().public_bytes_raw()).decode()}

    def key(self):
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(self.record["private_key"]))

    def prune(self):
        now = self.clock()
        for collection in (self.pending, self.challenges, self.sessions):
            for key, value in list(collection.items()):
                if value["expires_at"] <= now:
                    del collection[key]

    def pair(self, origin, permissions):
        with self.lock:
            self.identity()
            self.prune()
            if not permissions or not set(permissions) <= PERMISSIONS:
                raise HTTPException(422, "Permissions de lecture seule invalides")
            if len(self.pending) >= 20:
                raise HTTPException(429, "Trop de demandes en attente")
            token = secrets.token_urlsafe(32)
            pair_id = str(uuid4())
            self.pending[pair_id] = {"origin": origin, "permissions": sorted(set(permissions)), "token_hash": digest(token), "status": "pending", "expires_at": self.clock() + PAIR_TTL}
            return {"pair_id": pair_id, "pairing_token": token, "status": "pending", "expires_at": self.pending[pair_id]["expires_at"], "identity": self.identity()}

    def decide(self, pair_id, approve):
        with self.lock:
            self.prune()
            pending = self.pending.get(pair_id)
            if not pending or pending["status"] != "pending":
                raise HTTPException(404, "Demande indisponible")
            pending["status"] = "approved" if approve else "denied"
            if approve:
                self.record["grants"][pair_id] = {k: pending[k] for k in ("origin", "permissions", "token_hash")}
                try:
                    self._save()
                except Exception:
                    self.record["grants"].pop(pair_id, None)
                    pending["status"] = "pending"
                    raise
            return {"status": pending["status"]}

    def grant(self, origin, token):
        self.identity()
        for pair_id, grant in self.record["grants"].items():
            if grant["origin"] == origin and secrets.compare_digest(grant["token_hash"], digest(token)):
                return pair_id, grant
        raise HTTPException(401, "Appairage requis")

    def challenge(self, origin, token, nonce):
        with self.lock:
            self.prune()
            pair_id, grant = self.grant(origin, token)
            if len(self.challenges) >= 100:
                raise HTTPException(429, "Trop de challenges")
            challenge_id = str(uuid4())
            expires = self.clock() + 60
            payload = {"protocol": "etu-ai-session-v1", "instance_id": self.record["instance_id"], "pair_id": pair_id, "origin": origin, "permissions": grant["permissions"], "nonce": nonce, "challenge_id": challenge_id, "expires_at": expires}
            message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            self.challenges[challenge_id] = {"pair_id": pair_id, "expires_at": expires}
            return {"challenge_id": challenge_id, "signed_payload": base64.b64encode(message).decode(), "signature": base64.b64encode(self.key().sign(message)).decode(), "expires_at": expires}

    def session(self, origin, token, challenge_id):
        with self.lock:
            self.prune()
            pair_id, grant = self.grant(origin, token)
            challenge = self.challenges.get(challenge_id)
            if not challenge or challenge["pair_id"] != pair_id:
                raise HTTPException(401, "Challenge expiré ou déjà utilisé")
            if len(self.sessions) >= 100:
                raise HTTPException(429, "Trop de sessions")
            del self.challenges[challenge_id]
            session_token = secrets.token_urlsafe(32)
            expires = self.clock() + SESSION_TTL
            self.sessions[digest(session_token)] = {"pair_id": pair_id, "origin": origin, "permissions": grant["permissions"][:], "expires_at": expires, "context": {}}
            return {"access_token": session_token, "token_type": "Bearer", "expires_at": expires, "permissions": grant["permissions"]}

    def authenticate(self, origin, token):
        with self.lock:
            self.prune()
            session = self.sessions.get(digest(token))
            if not session or session["origin"] != origin:
                raise HTTPException(401, "Session absente ou expirée")
            return session

    def revoke(self, pair_id):
        with self.lock:
            self.identity()
            grant = self.record["grants"].pop(pair_id, None)
            try:
                self._save()
            except Exception:
                if grant:
                    self.record["grants"][pair_id] = grant
                raise
            self.pending.pop(pair_id, None)
            for collection in (self.sessions, self.challenges):
                for key, value in list(collection.items()):
                    if value["pair_id"] == pair_id:
                        del collection[key]


bridge = Bridge()
