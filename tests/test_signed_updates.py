import base64
import hashlib
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from integration.updates import RELEASE_SIGNING_PUBLIC_KEY, download_verified_release, verify_release, verify_published_installer, fetch_signed_installer_manifest
from tools.sign_release import create_manifest, main as sign_release_main, validate_release_signing_key


class TestSignedUpdates(unittest.TestCase):
    def test_embedded_release_public_key_matches_the_provided_ed25519_key(self):
        self.assertEqual(base64.b64encode(RELEASE_SIGNING_PUBLIC_KEY).decode(), "qcUmvreKLqUGwSUnw9duFfuq/k9NH8woTbhiP1KT80Y=")

    def test_published_manifest_binds_platform_name_size_url_and_digest(self):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw()
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "ETU-AI-Setup-1.2.0.exe"
            package.write_bytes(b"setup payload")
            url = "https://github.com/etu-aix/ETU-AI/releases/download/v1.2.0/" + package.name
            manifest = create_manifest(package, "v1.2.0", url, key)
            asset = {"name": package.name, "url": url, "size": package.stat().st_size,
                     "digest": "sha256:" + manifest["sha256"]}
            with patch("integration.updates.RELEASE_SIGNING_PUBLIC_KEY", public):
                self.assertEqual(verify_published_installer(asset, manifest, "v1.2.0", "Windows")["size"], package.stat().st_size)
                altered = {**manifest, "platform": "linux"}
                payload = {key: altered[key] for key in ("version", "name", "platform", "url", "size", "sha256")}
                altered["signature"] = base64.b64encode(key.sign(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())).decode()
                with self.assertRaisesRegex(ValueError, "ne correspond pas"):
                    verify_published_installer(asset, altered, "v1.2.0", "Windows")

    def test_release_sidecar_is_fetched_from_the_installer_release_path(self):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw()
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "ETU-AI-Setup-1.2.0.exe"
            package.write_bytes(b"setup payload")
            url = "https://github.com/etu-aix/ETU-AI/releases/download/v1.2.0/" + package.name
            manifest = create_manifest(package, "v1.2.0", url, key)
            assets = [
                {"name": package.name, "url": url, "size": package.stat().st_size, "digest": "sha256:" + manifest["sha256"]},
                {"name": package.name + ".manifest.json", "url": url + ".manifest.json"},
            ]
            with patch("integration.updates.RELEASE_SIGNING_PUBLIC_KEY", public), patch("integration.updates.urlopen", return_value=io.BytesIO(json.dumps(manifest).encode())) as fetch:
                checked = fetch_signed_installer_manifest(assets, assets[0], "v1.2.0", "Windows")
            self.assertEqual(checked, manifest)
            self.assertEqual(verify_release(checked, package, public, expected_version="v1.2.0")["name"], package.name)
            self.assertEqual(fetch.call_args.args[0].full_url, assets[1]["url"])
            with self.assertRaisesRegex(ValueError, "manifeste Ed25519"):
                fetch_signed_installer_manifest(assets[:1], assets[0], "v1.2.0", "Windows")
    def test_release_publisher_signs_compatible_manifest(self):
        key = Ed25519PrivateKey.generate()
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / 'setup.exe'
            package.write_bytes(b'installer')
            manifest = create_manifest(package, '1.2.0', 'https://github.com/etu-aix/ETU-AI/releases/download/v1.2.0/setup.exe', key)
            self.assertEqual(verify_release(manifest, package, key.public_key().public_bytes_raw(), expected_version='1.2.0')['version'], '1.2.0')
            with self.assertRaises(ValueError):
                create_manifest(package, '1.2.0', 'http://github.com/installer', key)

    def test_manifest_verifier_supports_overlapping_key_rotation(self):
        active = Ed25519PrivateKey.generate()
        next_key = Ed25519PrivateKey.generate()
        old_public = active.public_key().public_bytes_raw()
        new_public = next_key.public_key().public_bytes_raw()
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "ETU-AI-Setup-1.2.0.exe"
            package.write_bytes(b"installer")
            manifest = create_manifest(package, "v1.2.0", "https://github.com/etu-aix/ETU-AI/releases/download/v1.2.0/ETU-AI-Setup-1.2.0.exe", next_key)
            from integration.updates import verify_release_manifest
            verified = verify_release_manifest(manifest, (old_public, new_public), expected_version="v1.2.0")
            self.assertEqual(verified["version"], "v1.2.0")
            with self.assertRaisesRegex(ValueError, "Signature"):
                verify_release_manifest(manifest, (old_public,), expected_version="v1.2.0")

    def test_release_signer_must_match_a_client_pinned_key(self):
        trusted = Ed25519PrivateKey.generate()
        other = Ed25519PrivateKey.generate()
        trusted_public = trusted.public_key().public_bytes_raw()
        validate_release_signing_key(trusted, (trusted_public,))
        with self.assertRaisesRegex(ValueError, "clé publique intégrée"):
            validate_release_signing_key(other, (trusted_public,))

    def test_signer_cli_check_mode_accepts_a_matching_client_key(self):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw()
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "private.pem"
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            with patch("integration.updates.RELEASE_SIGNING_PUBLIC_KEY", public), \
                 patch("integration.updates.RELEASE_SIGNING_PUBLIC_KEY_ROTATION_KEYS", ()), \
                 patch("sys.argv", ["sign_release.py", "--check-key", "--private-key", str(key_path)]):
                sign_release_main()

    def test_signer_cli_writes_a_manifest_verified_by_the_embedded_key(self):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "ETU-AI-Setup-1.2.0.exe"
            package.write_bytes(b"installer")
            key_path, output = root / "private.pem", root / "manifest.json"
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            url = "https://github.com/etu-aix/ETU-AI/releases/download/v1.2.0/" + package.name
            with patch("integration.updates.RELEASE_SIGNING_PUBLIC_KEY", public), \
                 patch("integration.updates.RELEASE_SIGNING_PUBLIC_KEY_ROTATION_KEYS", ()), \
                 patch("sys.argv", ["sign_release.py", "--package", str(package), "--version", "v1.2.0", "--url", url,
                                     "--private-key", str(key_path), "--output", str(output)]):
                sign_release_main()
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(verify_release(manifest, package, public, expected_version="v1.2.0")["version"], "v1.2.0")
    def test_download_validates_before_network_and_keeps_old_package_on_failure(self):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw()
        payload = {'version': '1.0.1', 'name': 'setup.exe', 'platform': 'windows', 'size': 3,
                   'url': 'https://github.com/etu-aix/ETU-AI/releases/download/v1.0.1/setup.exe', 'sha256': hashlib.sha256(b'new').hexdigest()}
        signature = base64.b64encode(key.sign(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode())).decode()
        manifest = {**payload, 'signature': signature}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'setup.exe'
            destination.write_bytes(b'old')
            with patch('integration.updates.urlopen') as network:
                with self.assertRaises(ValueError):
                    download_verified_release({**manifest, 'url': 'https://evil.example'}, destination, public, expected_version='1.0.1')
                network.assert_not_called()
            for content, max_bytes in [(b'bad', 100), (b'new', 2)]:
                with patch('integration.updates.urlopen', return_value=io.BytesIO(content)):
                    with self.assertRaises(ValueError):
                        download_verified_release(manifest, destination, public, expected_version='1.0.1', max_bytes=max_bytes)
                self.assertEqual(destination.read_bytes(), b'old')
                self.assertEqual([p.name for p in Path(directory).iterdir()], ['setup.exe'])
            with patch('integration.updates.urlopen', return_value=io.BytesIO(b'new')):
                download_verified_release(manifest, destination, public, expected_version='1.0.1')
            self.assertEqual(destination.read_bytes(), b'new')

    def test_signed_package_and_tampering(self):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw()
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / 'release.bin'
            package.write_bytes(b'test package')
            payload = {'version': '1.0.1', 'name': package.name, 'platform': 'windows', 'size': package.stat().st_size,
                       'url': 'https://etu-aix.alwaysdata.net/releases/release.bin', 'sha256': hashlib.sha256(package.read_bytes()).hexdigest()}
            signature = base64.b64encode(key.sign(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode())).decode()
            manifest = {**payload, 'signature': signature}
            self.assertEqual(verify_release(manifest, package, public, expected_version='1.0.1'), payload)
            for altered in [payload, {**manifest, 'url': 'https://evil.example/app.bin'}, {**manifest, 'signature': 'bad'}, {**manifest, 'version': '1.0.0'}]:
                with self.assertRaises(ValueError):
                    verify_release(altered, package, public, expected_version='1.0.1')
            with self.assertRaises(ValueError):
                verify_release(manifest, package, Ed25519PrivateKey.generate().public_key().public_bytes_raw(), expected_version='1.0.1')
            package.write_bytes(b'altered package')
            with self.assertRaises(ValueError):
                verify_release(manifest, package, public, expected_version='1.0.1')
