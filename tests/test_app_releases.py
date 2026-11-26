import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from integration.updates import GITHUB_LATEST_API, RELEASES_URL, latest_app_release, is_newer_version, select_app_installer, download_verified_app_installer


class AppReleaseTests(unittest.TestCase):
    def test_latest_stable_release_uses_official_endpoint_and_link(self):
        github_asset = {"name": "ETU-AI-Setup-2.0.0.exe", "browser_download_url": "https://github.com/etu-aix/ETU-AI/releases/download/v2.0.0/ETU-AI-Setup-2.0.0.exe", "size": 1024, "digest": "sha256:" + "a" * 64}
        response = io.BytesIO(json.dumps({"tag_name": "v2.0.0", "html_url": "https://evil.example", "assets": [github_asset]}).encode())
        with patch("integration.updates.urlopen", return_value=response) as request:
            self.assertEqual(latest_app_release(), {"status": "available", "version": "v2.0.0", "url": RELEASES_URL,
                "assets": [{"name": github_asset["name"], "url": github_asset["browser_download_url"], "size": 1024, "digest": github_asset["digest"]}]})
        self.assertEqual(request.call_args.args[0].full_url, GITHUB_LATEST_API)

    def test_latest_release_rejects_asset_urls_outside_the_repository_release_path(self):
        assets = [
            {"name": "other-owner", "browser_download_url": "https://github.com/other/ETU-AI/releases/download/v2.0.0/setup.exe"},
            {"name": "credentials", "browser_download_url": "https://user@github.com/etu-aix/ETU-AI/releases/download/v2.0.0/setup.exe"},
            {"name": "query", "browser_download_url": "https://github.com/etu-aix/ETU-AI/releases/download/v2.0.0/setup.exe?redirect=evil"},
            {"name": "valid", "browser_download_url": "https://github.com/etu-aix/ETU-AI/releases/download/v2.0.0/setup.exe"},
        ]
        response = io.BytesIO(json.dumps({"tag_name": "v2.0.0", "assets": assets}).encode())
        with patch("integration.updates.urlopen", return_value=response):
            result = latest_app_release()
        self.assertEqual([item["name"] for item in result["assets"]], ["valid"])

    def test_selects_only_an_exact_platform_and_version_installer(self):
        assets = [
            {"name": "ETU-AI-Setup-2.0.0.exe", "url": "https://github.com/etu-aix/ETU-AI/releases/download/v2.0.0/ETU-AI-Setup-2.0.0.exe"},
            {"name": "ETU-AI-Setup-2.0.0-macos.dmg", "url": "https://github.com/etu-aix/ETU-AI/releases/download/v2.0.0/ETU-AI-Setup-2.0.0-macos.dmg"},
            {"name": "ETU-AI-Setup-2.0.0-linux.deb", "url": "https://github.com/etu-aix/ETU-AI/releases/download/v2.0.0/ETU-AI-Setup-2.0.0-linux.deb"},
            {"name": "ETU-AI-Setup-1.9.0.exe", "url": "https://github.com/etu-aix/ETU-AI/releases/download/v1.9.0/setup.exe"},
        ]
        self.assertEqual(select_app_installer(assets, "Windows", "v2.0.0"), assets[0])
        self.assertEqual(select_app_installer(assets, "Darwin", "v2.0.0"), assets[1])
        self.assertEqual(select_app_installer(assets, "Linux", "v2.0.0"), assets[2])
        self.assertIsNone(select_app_installer(assets, "Windows", "v2.1.0"))

    def test_installer_download_is_released_only_after_sha256_verification(self):
        payload = b"verified fake setup"
        with tempfile.TemporaryDirectory() as temp_dir:
            asset = {
                "name": "ETU-AI-Setup-2.0.0.exe",
                "url": "https://github.com/etu-aix/ETU-AI/releases/download/v2.0.0/ETU-AI-Setup-2.0.0.exe",
                "size": len(payload),
                "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            }
            with patch("integration.updates.tempfile.gettempdir", return_value=temp_dir), \
                 patch("integration.updates.urlopen", return_value=io.BytesIO(payload)):
                downloaded = download_verified_app_installer(asset, "v2.0.0")
            self.assertEqual(downloaded.read_bytes(), payload)
            downloaded.unlink()

            asset["digest"] = "sha256:" + "0" * 64
            with patch("integration.updates.tempfile.gettempdir", return_value=temp_dir), \
                 patch("integration.updates.urlopen", return_value=io.BytesIO(payload)):
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    download_verified_app_installer(asset, "v2.0.0")
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_installer_download_rejects_untrusted_url_before_network(self):
        payload = b"fake setup"
        asset = {
            "name": "ETU-AI-Setup-2.0.0.exe",
            "url": "https://evil.example/ETU-AI-Setup-2.0.0.exe",
            "size": len(payload),
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        }
        with patch("integration.updates.urlopen") as request:
            with self.assertRaisesRegex(ValueError, "URL"):
                download_verified_app_installer(asset, "v2.0.0")
        request.assert_not_called()

    def test_installer_download_rejects_a_filename_for_another_version(self):
        asset = {
            "name": "ETU-AI-Setup-1.9.0.exe",
            "url": "https://github.com/etu-aix/ETU-AI/releases/download/v2.0.0/ETU-AI-Setup-1.9.0.exe",
            "size": 10,
            "digest": "sha256:" + "0" * 64,
        }
        with patch("integration.updates.urlopen") as request:
            with self.assertRaisesRegex(ValueError, "version du nom"):
                download_verified_app_installer(asset, "v2.0.0")
        request.assert_not_called()

    def test_missing_release_is_distinct_from_network_failure(self):
        with patch("integration.updates.urlopen", side_effect=HTTPError(GITHUB_LATEST_API, 404, "missing", {}, None)):
            self.assertEqual(latest_app_release()["status"], "unavailable")
        with patch("integration.updates.urlopen", side_effect=URLError("offline")):
            with self.assertRaises(ValueError):
                latest_app_release()

    def test_invalid_and_oversized_response_are_rejected(self):
        for body in (b"{}", b"x" * (1024 * 1024 + 1)):
            with self.subTest(size=len(body)), patch("integration.updates.urlopen", return_value=io.BytesIO(body)):
                with self.assertRaises(ValueError):
                    latest_app_release()



class VersionComparisonTests(unittest.TestCase):
    def test_compares_numeric_release_versions(self):
        self.assertTrue(is_newer_version("v1.10.0", "v1.9.9"))
        self.assertFalse(is_newer_version("v1.2.0", "1.2"))
        self.assertFalse(is_newer_version("v1.0.0", "v2.0.0"))
        self.assertFalse(is_newer_version("v3.0.0", None))
        self.assertFalse(is_newer_version("v3.0.0-rc1", "v2.0.0"))
