"""Create an Ed25519 manifest for a release asset using an external private key."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def create_manifest(package: Path, version: str, url: str, private_key: Ed25519PrivateKey) -> dict:
    if not version or len(version) > 128:
        raise ValueError("Version invalide")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL HTTPS requise")
    digest = hashlib.sha256()
    with package.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    payload = {"version": version, "url": url, "sha256": digest.hexdigest()}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {**payload, "signature": base64.b64encode(private_key.sign(encoded)).decode("ascii")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--private-key", type=Path, required=True, help="Clé Ed25519 PEM, conservée hors du dépôt")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    key = serialization.load_pem_private_key(args.private_key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Clé privée Ed25519 requise")
    manifest = create_manifest(args.package, args.version, args.url, key)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


if __name__ == "__main__":
    main()
