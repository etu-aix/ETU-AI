"""Local ETU identity, explicit grants and volatile, origin-bound sessions."""
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from uuid import uuid4
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
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
    "etu.workshop.profiles",
}
SESSION_TTL = 1800
PAIR_TTL = 300
_LOGGER = logging.getLogger(__name__)
_DPAPI_BAD_KEY_STATE = {-2146893813, 0x8009000B}
_IDENTITY_MAGIC = b"ETU-AI-IDENTITY\x01"
_IDENTITY_KEY_NAME = "bridge:identity-encryption-key:v1"


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
        default_path = Path(__file__).resolve().parent.parent / "storage"
        configured_path = path or os.environ.get("ETU_AI_IDENTITY_FILE")
        self.path = Path(configured_path or default_path / "etu_identity.vault")
        self.legacy_path = default_path / "etu_identity.dpapi" if not configured_path else None
        self.clock = clock
        self.lock = threading.RLock()
        self.record = None
        self._legacy_source = None
        self._migrate_required = False
        self.pending = {}
        self.challenges = {}
        self.sessions = {}
        self.admin_token = secrets.token_urlsafe(32)

    def _keyring(self):
        import keyring
        return keyring

    def _encryption_key(self, *, create):
        keyring = self._keyring()
        value = keyring.get_password("ETU-AI", _IDENTITY_KEY_NAME)
        if value is not None:
            key = base64.b64decode(value, validate=True)
            if len(key) != 32:
                raise ValueError("La clé du coffre d’identité ETU-AI a un format invalide.")
            return key
        if not create:
            return None
        key = secrets.token_bytes(32)
        keyring.set_password("ETU-AI", _IDENTITY_KEY_NAME, base64.b64encode(key).decode("ascii"))
        return key

    def _archive(self, path, reason):
        if not path or not path.exists():
            return None
        backup = path.with_name(f"{path.stem}.{reason}-{uuid4().hex}{path.suffix}")
        with backup.open("xb") as handle:
            handle.write(path.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        _LOGGER.warning("Archived previous ETU identity as %s (%s)", backup.name, reason)
        return backup

    def _recover_unreadable(self, path, reason):
        self._archive(path, reason)
        return None

    def _read(self):
        source = self.path if self.path.exists() else self.legacy_path
        if source and source.exists():
            encrypted = source.read_bytes()
            if encrypted.startswith(_IDENTITY_MAGIC):
                key = self._encryption_key(create=False)
                if key is None:
                    return self._recover_unreadable(source, "key-missing")
                try:
                    nonce = encrypted[len(_IDENTITY_MAGIC):len(_IDENTITY_MAGIC) + 12]
                    ciphertext = encrypted[len(_IDENTITY_MAGIC) + 12:]
                    raw = AESGCM(key).decrypt(nonce, ciphertext, _IDENTITY_MAGIC)
                except (InvalidTag, ValueError):
                    return self._recover_unreadable(source, "unreadable")
                if source != self.path:
                    self._legacy_source = source
                    self._migrate_required = True
                return raw
            if os.name == "nt":
                try:
                    raw = _dpapi(encrypted, decrypt=True)
                except OSError as exc:
                    if getattr(exc, "winerror", None) not in _DPAPI_BAD_KEY_STATE:
                        raise
                    return self._recover_unreadable(source, "unreadable")
                self._legacy_source = source
                self._migrate_required = True
                return raw
            return self._recover_unreadable(source, "unsupported")

        # Previous non-Windows releases stored the JSON record directly in
        # keyring. Migrate that record to the same encrypted-file format.
        old_path = self.legacy_path or self.path
        value = self._keyring().get_password("ETU-AI", "bridge:" + str(old_path.resolve()))
        self._migrate_required = bool(value)
        return value.encode() if value else None

    def _save(self):
        raw = json.dumps(self.record, separators=(",", ":")).encode()
        key = self._encryption_key(create=True)
        nonce = secrets.token_bytes(12)
        encrypted = _IDENTITY_MAGIC + nonce + AESGCM(key).encrypt(nonce, raw, _IDENTITY_MAGIC)
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

    def _load_record(self, raw):
        try:
            record = json.loads(raw)
            if (not isinstance(record, dict) or not isinstance(record.get("instance_id"), str)
                    or not isinstance(record.get("private_key"), str) or not isinstance(record.get("grants"), dict)):
                raise ValueError("invalid identity record")
            Ed25519PrivateKey.from_private_bytes(base64.b64decode(record["private_key"], validate=True))
            return record
        except (TypeError, ValueError, json.JSONDecodeError):
            self._recover_unreadable(self._legacy_source or self.path, "invalid")
            self._legacy_source = None
            return None

    def identity(self):
        with self.lock:
            if self.record is None:
                raw = self._read()
                if raw:
                    self.record = self._load_record(raw)
                if self.record is None:
                    key = Ed25519PrivateKey.generate()
                    self.record = {"instance_id": str(uuid4()), "private_key": base64.b64encode(key.private_bytes_raw()).decode(), "grants": {}}
                    try:
                        self._save()
                    except Exception:
                        self.record = None
                        raise
                elif self._migrate_required:
                    if self._legacy_source:
                        self._archive(self._legacy_source, "migrated")
                    self._save()
                    self._legacy_source = None
                    self._migrate_required = False
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
