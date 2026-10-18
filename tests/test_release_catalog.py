"""Release discovery must offer only real archives for the current host."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai-server"))

from release_catalog import parse_release_assets, release_variants


REPO = "MAIA-404-systems/llama-cpp-turboquant"


def asset(name, tag="v1.0.2", digest="sha256:" + "a" * 64):
    return {"name": name, "digest": digest,
            "browser_download_url": f"https://github.com/{REPO}/releases/download/{tag}/{name}"}


class ReleaseCatalogTests(unittest.TestCase):
    def test_all_platforms_are_listed_but_only_matching_host_is_compatible(self):
        cpu = "llama-server-v1.0.2-linux-x64-cpu.tar.gz"
        hybrid = "llama-server-v1.0.2-linux-x64-vulkan-openvino.tar.gz"
        releases = [{"tag_name": "v1.0.2", "assets": [
            asset(cpu), asset(cpu), asset(hybrid), asset("llama-server-v1.0.2-windows-x64-cpu.zip"),
            {**asset("llama-server-v1.0.2-linux-x64-cuda12.4.tar.gz"), "browser_download_url": "https://evil.example/archive"},
        ]}, {"tag_name": "v1.0.1", "draft": True, "assets": [asset("llama-server-v1.0.1-linux-x64-cpu.tar.gz", "v1.0.1")]}]
        result = parse_release_assets(releases, REPO, "linux", "x64", "tar.gz")
        self.assertEqual([item["id"] for item in result], [cpu, hybrid, "llama-server-v1.0.2-windows-x64-cpu.zip"])
        self.assertEqual(result[1]["variant"], "vulkan-openvino")
        self.assertEqual(result[0]["sha256"], "a" * 64)
        self.assertEqual([item["compatible"] for item in result], [True, True, False])

    def test_repository_name_is_validated_before_fetch(self):
        with patch("release_catalog.safe_urlopen") as fetch:
            with self.assertRaisesRegex(ValueError, "invalide"):
                release_variants({"llama_release_repo": "https://evil.example/repo"})
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
