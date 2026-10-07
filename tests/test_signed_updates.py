import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from integration.updates import verify_release


class TestSignedUpdates(unittest.TestCase):
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
