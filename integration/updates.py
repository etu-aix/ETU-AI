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

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


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
