"""Local Sign in with ChatGPT session for the OpenAI Responses API."""

import asyncio
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils
from cryptography.hazmat.primitives import hashes

from storage.api_credentials import get_api_key, set_api_key

AUTH_BASE = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
VAULT_ID = "chatgpt-oauth"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
_pending = {}
_result = {"status": "idle"}
_current_state = ""
_refresh_lock = asyncio.Lock()


def fail(state, message):
    global _result
    if state and secrets.compare_digest(state, _current_state):
        _result = {"status": "error", "message": message}


def _load():
    raw = get_api_key(VAULT_ID)
    if not raw:
        return {"accounts": {}, "active": None}
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
        raise ValueError("Les identifiants ChatGPT enregistrés sont invalides.")
    return data


def _save(data):
    set_api_key(json.dumps(data, separators=(",", ":")), VAULT_ID)


def status():
    data = _load()
    accounts = [{"client_id": key, "email": value.get("email", ""), "connected": bool(value.get("refresh_token"))}
                for key, value in data["accounts"].items()]
    return {"accounts": accounts, "active": data.get("active"), "flow": _result}


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def start(storage, port, client_id=None):
    global _result, _current_state
    data = _load()
    account = data["accounts"].get(client_id) if client_id else None
    if client_id and not account:
        raise ValueError("Ce compte ChatGPT n'est pas enregistré.")
    host_id = storage.get_setting("chatgpt_host_id")
    if not host_id:
        host_id = f"urn:uuid:{uuid4()}"
        storage.set_setting("chatgpt_host_id", host_id)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    redirect_uri = f"http://127.0.0.1:{port}/auth/callback"
    _pending.clear()
    _pending[state] = {"nonce": nonce, "verifier": verifier, "redirect_uri": redirect_uri,
                       "client_id": client_id, "started": time.monotonic()}
    _current_state = state
    params = {
        "client_id": client_id or "dynamic_agent_client",
        "ext_agent_host_id": host_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
    }
    if account:
        if account.get("email"):
            params["login_hint"] = account["email"]
    else:
        params["agent_name_hint"] = "ETU-AI"
    _result = {"status": "pending"}
    return f"{AUTH_BASE}/api/accounts/authorize?{urlencode(params)}"


def _verify_id_token(token, jwks, client_id, nonce):
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Jeton d'identité mal formé.")
    def decode(part):
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    header, claims = decode(parts[0]), decode(parts[1])
    if header.get("alg") not in {"RS256", "ES256"}:
        raise ValueError("Algorithme de signature non pris en charge.")
    key = next((item for item in jwks.get("keys", []) if item.get("kid") == header.get("kid") and
                item.get("kty") == ("RSA" if header["alg"] == "RS256" else "EC")), None)
    if not key:
        raise ValueError("Clé de signature OpenAI introuvable.")
    number = lambda value: int.from_bytes(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)), "big")
    signature = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
    signed_content = f"{parts[0]}.{parts[1]}".encode()
    if header["alg"] == "RS256":
        public_key = rsa.RSAPublicNumbers(number(key["e"]), number(key["n"])).public_key()
        public_key.verify(signature, signed_content, padding.PKCS1v15(), hashes.SHA256())
    else:
        if key.get("crv") != "P-256" or len(signature) != 64:
            raise ValueError("Clé de signature OpenAI invalide.")
        public_key = ec.EllipticCurvePublicNumbers(number(key["x"]), number(key["y"]), ec.SECP256R1()).public_key()
        der_signature = utils.encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
        public_key.verify(der_signature, signed_content, ec.ECDSA(hashes.SHA256()))
    audience = claims.get("aud")
    if (claims.get("iss") != AUTH_BASE or
            client_id not in (audience if isinstance(audience, list) else [audience]) or
            claims.get("nonce") != nonce or
            not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= time.time() or
            not isinstance(claims.get("iat"), (int, float)) or claims["iat"] > time.time() + 5 or
            not isinstance(claims.get("sub"), str)):
        raise ValueError("L'identité ChatGPT n'a pas pu être validée.")
    return claims


async def complete(query):
    global _result
    state = query.get("state", "")
    pending = _pending.pop(state, None)
    if not pending or time.monotonic() - pending["started"] > 600:
        raise ValueError("Session de connexion expirée ou invalide.")
    if query.get("error"):
        _result = {"status": "error", "message": "Connexion refusée ou annulée."}
        return
    code = query.get("code")
    client_id = query.get("client_id") or pending["client_id"]
    if not code or not client_id or client_id == "dynamic_agent_client":
        raise ValueError("OpenAI n'a pas fourni le code ou l'identifiant client attendu.")
    if pending["client_id"] and pending["client_id"] != client_id:
        raise ValueError("Le compte retourné ne correspond pas au compte demandé.")
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.post(f"{AUTH_BASE}/api/accounts/oauth/token", data={
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "code_verifier": pending["verifier"], "redirect_uri": pending["redirect_uri"], "resource": RESOURCE,
        })
        response.raise_for_status()
        tokens = response.json()
        discovery = await client.get(f"{AUTH_BASE}/.well-known/openid-configuration")
        discovery.raise_for_status()
        metadata = discovery.json()
        jwks_uri = metadata.get("jwks_uri", "")
        if not jwks_uri.startswith(f"{AUTH_BASE}/"):
            raise ValueError("Adresse des clés OpenAI invalide.")
        jwks_response = await client.get(jwks_uri)
        jwks_response.raise_for_status()
    claims = _verify_id_token(tokens["id_token"], jwks_response.json(), client_id, pending["nonce"])
    scopes = tokens.get("scope", "").split()
    if "chatgpt.tokens.use.direct" not in scopes or "resource.invoke" not in scopes:
        raise ValueError("L'accès au forfait ChatGPT n'a pas été autorisé.")
    data = _load()
    existing = data["accounts"].get(client_id)
    if existing and existing.get("subject") != claims["sub"]:
        raise ValueError("Ce compte ne correspond pas à l'identité enregistrée.")
    data["accounts"][client_id] = {
        "subject": claims["sub"], "email": claims.get("email", ""), "client_id": client_id,
        "id_token": tokens["id_token"], "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"], "scopes": scopes,
        "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
    }
    data["active"] = client_id
    _save(data)
    _result = {"status": "success"}


async def access_token():
    async with _refresh_lock:
        data = _load()
        client_id = data.get("active")
        account = data["accounts"].get(client_id) if client_id else None
        if not account or not account.get("refresh_token"):
            raise ValueError("Connectez d'abord votre compte ChatGPT.")
        if account.get("expires_at", 0) > time.time() + 120:
            return account["access_token"]
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.post(f"{AUTH_BASE}/api/accounts/oauth/token", data={
                "grant_type": "refresh_token", "client_id": client_id,
                "refresh_token": account["refresh_token"], "resource": RESOURCE,
            })
            response.raise_for_status()
            tokens = response.json()
        scopes = (tokens.get("scope") or " ".join(account.get("scopes", []))).split()
        if "chatgpt.tokens.use.direct" not in scopes or "resource.invoke" not in scopes:
            raise ValueError("Le forfait ChatGPT n'autorise plus les requêtes d'ETU-AI.")
        account.update(access_token=tokens["access_token"], refresh_token=tokens["refresh_token"],
                       scopes=scopes, expires_at=time.time() + int(tokens.get("expires_in", 3600)))
        _save(data)
        return account["access_token"]


def select(client_id):
    data = _load()
    account = data["accounts"].get(client_id)
    if not account or not account.get("refresh_token"):
        raise ValueError("Ce compte ChatGPT n'est pas connecté.")
    data["active"] = client_id
    _save(data)


async def disconnect():
    data = _load()
    client_id = data.get("active")
    account = data["accounts"].get(client_id) if client_id else None
    if not account:
        return True
    revoked = False
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            discovery = await client.get(f"{AUTH_BASE}/.well-known/openid-configuration")
            discovery.raise_for_status()
            endpoint = discovery.json().get("revocation_endpoint", "")
            if not endpoint.startswith(f"{AUTH_BASE}/"):
                raise ValueError("Adresse de révocation invalide.")
            response = await client.post(endpoint, data={"token": account["refresh_token"],
                                                      "token_type_hint": "refresh_token", "client_id": client_id})
            revoked = response.status_code == 200
    except (httpx.HTTPError, ValueError):
        revoked = False
    finally:
        for field in ("id_token", "access_token", "refresh_token", "expires_at", "scopes"):
            account.pop(field, None)
        data["active"] = None
        _save(data)
    return revoked
