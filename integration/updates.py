"""Fail-closed verification of release packages before an installer may run.

The release public key must be pinned by the distributor, independently of the
downloaded manifest and of the per-installation bridge identity.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
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
    tag = data["tag_name"][:128]
    assets = []
    for item in data.get("assets", [])[:100] if isinstance(data.get("assets", []), list) else []:
        if not isinstance(item, dict):
            continue
        name, download_url = item.get("name"), item.get("browser_download_url")
        parsed = urlsplit(download_url) if isinstance(download_url, str) else None
        if (not isinstance(name, str) or len(name) > 255 or not parsed or parsed.scheme != "https"
                or parsed.hostname != "github.com" or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path.split("/")[:5] != ["", "etu-aix", "ETU-AI", "releases", "download"]):
            continue
        assets.append({"name": name, "url": download_url[:2048], "size": item.get("size") if isinstance(item.get("size"), int) else None,
                       "digest": item.get("digest") if isinstance(item.get("digest"), str) else None})
    return {"status": "available", "version": tag, "url": RELEASES_URL, "assets": assets}


def select_app_installer(assets: list[dict], system: str, version: str) -> dict | None:
    """Select only an application installer explicitly named for this OS/version."""
    clean_version = version.removeprefix("v")
    suffixes = {
        "Windows": (".exe",),
        "Darwin": ("-macos.dmg", "-macos.pkg", "-macos.tar.gz"),
        "Linux": ("-linux.AppImage", "-linux.deb", "-linux.tar.gz"),
    }.get(system, ())
    expected_names = tuple(f"ETU-AI-Setup-{clean_version}{suffix}" for suffix in suffixes)
    for asset in assets:
        if isinstance(asset, dict) and asset.get("name") in expected_names and isinstance(asset.get("url"), str):
            return asset
    return None


def download_verified_app_installer(asset: dict, version: str, *, max_bytes: int = 2 * 1024 * 1024 * 1024) -> Path:
    """Download a GitHub release installer to a temporary file and verify its published digest."""
    if not isinstance(asset, dict):
        raise ValueError("Installateur GitHub invalide")
    name, url, digest, size = asset.get("name"), asset.get("url"), asset.get("digest"), asset.get("size")
    installer_match = re.fullmatch(r"ETU-AI-Setup-(\d+(?:\.\d+){1,3})(?:\.exe|-macos\.(?:dmg|pkg|tar\.gz)|-linux\.(?:AppImage|deb|tar\.gz))", name) if isinstance(name, str) else None
    if not installer_match:
        raise ValueError("Nom d’installateur invalide")
    clean_version = version.removeprefix("v")
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", clean_version) or installer_match.group(1) != clean_version:
        raise ValueError("La version du nom d’installateur ne correspond pas à la release")
    parsed = urlsplit(url) if isinstance(url, str) else None
    release_version = version if version.startswith("v") else f"v{version}"
    if (not parsed or parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or parsed.path.split("/") != ["", "etu-aix", "ETU-AI", "releases", "download", release_version, name]):
        raise ValueError("L’URL de l’installateur ne correspond pas à cette release GitHub")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise ValueError("GitHub ne fournit pas d’empreinte SHA-256 valide pour cet installateur")
    expected_hash = digest.removeprefix("sha256:").lower()
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or size > max_bytes:
        raise ValueError("Taille d’installateur invalide ou trop importante")

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=tempfile.gettempdir(), prefix="etu-app-installer-", suffix=Path(name).suffix, delete=False) as output:
            temp_path = Path(output.name)
            request = Request(url, headers={"User-Agent": "ETU-AI"})
            with urlopen(request, timeout=30) as response:
                checksum = hashlib.sha256()
                total = 0
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > min(size, max_bytes):
                        raise ValueError("Installateur téléchargé plus volumineux que la taille publiée par GitHub")
                    checksum.update(block)
                    output.write(block)
        if total != size:
            raise ValueError("Taille de l’installateur différente de celle annoncée par GitHub")
        if checksum.hexdigest() != expected_hash:
            raise ValueError("L’empreinte SHA-256 de l’installateur ne correspond pas à celle publiée par GitHub")
        verified_path = temp_path
        temp_path = None
        return verified_path
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ValueError("Impossible de télécharger l’installateur depuis GitHub.") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def installed_version() -> str | None:
    try:
        value = (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
        return value if re.fullmatch(r"v?\d+(?:\.\d+){1,3}", value) else None
    except OSError:
        return None


def is_newer_version(remote: str, installed: str | None) -> bool:
    def parts(value):
        if not value or not re.fullmatch(r"v?\d+(?:\.\d+){1,3}", value):
            return None
        numbers = tuple(map(int, value.lstrip("v").split(".")))
        return numbers + (0,) * (4 - len(numbers))
    latest, current = parts(remote), parts(installed)
    return latest is not None and current is not None and latest > current


def verify_release_manifest(manifest: dict, public_key: bytes, *, expected_version: str) -> dict:
    """Authenticate the URL and hash before making any network request."""
    if not isinstance(manifest, dict):
        raise ValueError("Manifest signé incomplet")
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
    return payload


def verify_release(manifest: dict, package: Path, public_key: bytes, *, expected_version: str) -> dict:
    payload = verify_release_manifest(manifest, public_key, expected_version=expected_version)
    checksum = hashlib.sha256()
    with Path(package).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
    if checksum.hexdigest() != payload["sha256"]:
        raise ValueError("Intégrité du paquet invalide")
    return payload


def download_verified_release(manifest: dict, destination: Path, public_key: bytes, *, expected_version: str,
                              max_bytes: int = 2 * 1024 * 1024 * 1024) -> dict:
    """Download to a temporary file, authenticate it, then atomically publish it."""
    payload = verify_release_manifest(manifest, public_key, expected_version=expected_version)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        request = Request(payload["url"], headers={"User-Agent": "ETU-AI"})
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".etu-update-", delete=False) as output:
            temp_path = Path(output.name)
            with urlopen(request, timeout=30) as response:
                total = 0
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > max_bytes:
                        raise ValueError("Paquet de mise à jour trop volumineux")
                    output.write(block)
        verify_release(manifest, temp_path, public_key, expected_version=expected_version)
        os.replace(temp_path, destination)
        temp_path = None
        return payload
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
