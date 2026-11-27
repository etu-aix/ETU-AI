"""Create an Ed25519 manifest for a release asset using an external private key."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import sys; sys.path.insert(0, str(Path(__file__).resolve().parents[1])); from integration.updates import release_signing_public_keys, verify_release
import sys; sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sys

def create_manifest(package: Path, version: str, url: str, private_key: Ed25519PrivateKey,
                    *, platform: str = "windows") -> dict:
    if not version or len(version) > 128:
        raise ValueError("Version invalide")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL HTTPS requise")
    if platform not in {"windows", "macos", "linux"} or Path(urlsplit(url).path).name != package.name:
        raise ValueError("Plateforme ou nom de paquet invalide")
    digest = hashlib.sha256()
    with package.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    payload = {"version": version, "name": package.name, "platform": platform,
               "url": url, "size": package.stat().st_size, "sha256": digest.hexdigest()}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {**payload, "signature": base64.b64encode(private_key.sign(encoded)).decode("ascii")}


def validate_release_signing_key(private_key: Ed25519PrivateKey, trusted_public_keys: tuple[bytes, ...]) -> None:
    public_key = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if public_key not in trusted_public_keys:
        raise ValueError("La clé privée de release ne correspond à aucune clé publique intégrée au client.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--url")
    parser.add_argument("--platform", choices=("windows", "macos", "linux"), default="windows")
    parser.add_argument("--private-key", type=Path, required=True, help="Clé Ed25519 PEM, conservée hors du dépôt")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-key", action="store_true", help="Vérifier seulement que la clé privée correspond à une clé publique de confiance")
    args = parser.parse_args()
    key = serialization.load_pem_private_key(args.private_key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Clé privée Ed25519 requise")
    validate_release_signing_key(key, release_signing_public_keys())
    if args.check_key:
        print("La clé privée correspond à une clé publique intégrée au client.")
        return
    if any(getattr(args, name) is None for name in ("package", "version", "url", "output")):
        parser.error("--check-key ou les options --package, --version, --url et --output sont requis")
    manifest = create_manifest(args.package, args.version, args.url, key, platform=args.platform)
    verify_release(manifest, args.package, release_signing_public_keys(), expected_version=args.version)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


if __name__ == "__main__":
    main()
