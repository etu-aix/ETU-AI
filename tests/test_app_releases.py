import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from integration.updates import GITHUB_LATEST_API, RELEASES_URL, latest_app_release, is_newer_version


class AppReleaseTests(unittest.TestCase):
    def test_latest_stable_release_uses_official_endpoint_and_link(self):
        response = io.BytesIO(json.dumps({"tag_name": "v2.0.0", "html_url": "https://evil.example"}).encode())
        with patch("integration.updates.urlopen", return_value=response) as request:
            self.assertEqual(latest_app_release(), {"status": "available", "version": "v2.0.0", "url": RELEASES_URL})
        self.assertEqual(request.call_args.args[0].full_url, GITHUB_LATEST_API)

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
