import base64
import hashlib
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from integration.updates import download_verified_release, verify_release
from tools.sign_release import create_manifest


class TestSignedUpdates(unittest.TestCase):
    def test_release_publisher_signs_compatible_manifest(self):
        key = Ed25519PrivateKey.generate()
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / 'setup.exe'
            package.write_bytes(b'installer')
            manifest = create_manifest(package, '1.2.0', 'https://github.com/etu-aix/ETU-AI/releases/download/v1.2.0/setup.exe', key)
            self.assertEqual(verify_release(manifest, package, key.public_key().public_bytes_raw(), expected_version='1.2.0')['version'], '1.2.0')
            with self.assertRaises(ValueError):
                create_manifest(package, '1.2.0', 'http://github.com/installer', key)
    def test_download_validates_before_network_and_keeps_old_package_on_failure(self):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw()
        payload = {'version': '1.0.1', 'url': 'https://github.com/etu-aix/ETU-AI/releases/download/v1.0.1/setup.exe', 'sha256': hashlib.sha256(b'new').hexdigest()}
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
            payload = {'version': '1.0.1', 'url': 'https://etu-aix.alwaysdata.net/releases/app.bin', 'sha256': hashlib.sha256(package.read_bytes()).hexdigest()}
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
