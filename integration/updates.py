"""Fail-closed verification of release packages before an installer may run.

The release public key must be pinned by the distributor, independently of the
downloaded manifest and of the per-installation bridge identity.
"""
import base64
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

RELEASES_URL = "https://github.com/etu-aix/ETU-AI/releases/latest"
GITHUB_LATEST_API = "https://api.github.com/repos/etu-aix/ETU-AI/releases/latest"


def latest_app_release() -> dict:
    """Read the latest published stable release without executing its assets."""
    request = Request(GITHUB_LATEST_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "ETU-AI"})
    try:
        with urlopen(request, timeout=10) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError("Réponse trop volumineuse")
            data = json.loads(body)
    except HTTPError as exc:
        if exc.code == 404:
            return {"status": "unavailable", "url": RELEASES_URL}
        raise ValueError("GitHub est momentanément indisponible.") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise ValueError("Impossible de consulter les releases GitHub.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tag_name"), str) or not data["tag_name"].strip():
        raise ValueError("La réponse de GitHub est invalide.")
    return {"status": "available", "version": data["tag_name"][:128], "url": RELEASES_URL}


def verify_release(manifest: dict, package: Path, public_key: bytes, *, expected_version: str) -> dict:
    required = {"version", "url", "sha256", "signature"}
    if set(manifest) != required or any(not isinstance(manifest[k], str) for k in required):
        raise ValueError("Manifest signé incomplet")
    payload = {key: manifest[key] for key in ("version", "url", "sha256")}
    if payload["version"] != expected_version:
        raise ValueError("Version de mise à jour inattendue")
    parsed = urlsplit(payload["url"])
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL de mise à jour HTTPS requise")
    if not re.fullmatch(r"[0-9a-f]{64}", payload["sha256"]):
        raise ValueError("SHA256 invalide")
    try:
        signed = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = base64.b64decode(manifest["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, signed)
    except (ValueError, InvalidSignature) as exc:
        raise ValueError("Signature de mise à jour invalide") from exc
    checksum = hashlib.sha256()
    with Path(package).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
    if checksum.hexdigest() != payload["sha256"]:
        raise ValueError("Intégrité du paquet invalide")
    return payload
