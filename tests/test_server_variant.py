"""The maintenance installer must not silently switch to another backend."""

import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai-server"))

from configs import DEFAULT_CONFIG
from llama_setup import download_precompiled_binary, setup_llama


class ServerVariantTests(unittest.TestCase):
    def test_exact_variant_does_not_try_fallback(self):
        config = {**DEFAULT_CONFIG, "_exact_variant": "cpu"}
        directory = Path(__file__).parent
        with patch("llama_setup.get_platform_info", return_value=("linux", "x64", "tar.gz", ["cuda", "cpu"])), \
             patch("llama_setup.tempfile.TemporaryDirectory", return_value=nullcontext(directory)), \
             patch("llama_setup.download", side_effect=RuntimeError("missing release")) as download:
            self.assertEqual(download_precompiled_binary(directory, "cuda", config), (None, None))
        self.assertEqual(download.call_count, 1)
        self.assertIn("-cpu.tar.gz", download.call_args.args[0])

    def test_exact_variant_does_not_compile_from_source(self):
        config = {**DEFAULT_CONFIG, "backend": "cpu"}
        with patch("llama_setup.read_json", return_value={}), \
             patch("llama_setup.get_platform_info"), \
             patch("llama_setup.download_precompiled_binary", return_value=(None, None)), \
             patch("llama_setup.compile_from_source") as compile_source:
            with self.assertRaisesRegex(RuntimeError, "indisponible"):
                setup_llama({"recommended_backend": "cpu", "npus": []}, config=config, exact_variant="cpu")
        compile_source.assert_not_called()

    def test_catalog_asset_uses_its_exact_archive_and_checksum(self):
        name = "llama-server-v1.0.2-linux-x64-vulkan-openvino.tar.gz"
        url = f"https://github.com/MAIA-404-systems/llama-cpp-turboquant/releases/download/v1.0.2/{name}"
        config = {**DEFAULT_CONFIG, "_exact_variant": "vulkan-openvino",
                  "_exact_asset_name": name, "_exact_asset_url": url,
                  "binary_sha256": {name: "a" * 64}}
        with patch("llama_setup.get_platform_info", return_value=("linux", "x64", "tar.gz", ["vulkan", "cpu"])), \
             patch("llama_setup.tempfile.TemporaryDirectory", return_value=nullcontext(Path(__file__).parent)), \
             patch("llama_setup.download", side_effect=RuntimeError("test stop")) as download:
            self.assertEqual(download_precompiled_binary(Path(__file__).parent, "vulkan", config), (None, None))
        download.assert_called_once()
        self.assertEqual(download.call_args.args[0], url)
        self.assertEqual(download.call_args.args[2], "a" * 64)


if __name__ == "__main__":
    unittest.main()
