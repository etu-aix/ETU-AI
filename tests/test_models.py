"""Tests unitaires pour le catalogue de modèles et l'évaluation matérielle."""
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models_mgr.catalog import RECOMMENDED_CATALOG, assess_compatibility, get_catalog_with_status


class TestModelCatalog(unittest.TestCase):

    def test_catalog_structure(self):
        self.assertGreaterEqual(len(RECOMMENDED_CATALOG), 3)
        for model in RECOMMENDED_CATALOG:
            self.assertIn("id", model)
            self.assertIn("filename", model)
            self.assertTrue(model["filename"].endswith(".gguf"))
            self.assertIn("required_ram_gib", model)

    def test_compatibility_gpu(self):
        hw = {
            "recommended_backend": "cuda",
            "gpus": [{"type": "cuda", "name": "NVIDIA GeForce RTX 4070"}],
            "cpu": {"count": 8},
        }
        res = assess_compatibility(RECOMMENDED_CATALOG[0], hw)
        self.assertEqual(res["color"], "success")
        self.assertTrue(res["has_gpu_acceleration"])

    def test_compatibility_cpu(self):
        hw = {
            "recommended_backend": "cpu",
            "gpus": [],
            "cpu": {"count": 4},
        }
        res = assess_compatibility(RECOMMENDED_CATALOG[0], hw)
        self.assertEqual(res["color"], "success")
        self.assertFalse(res["has_gpu_acceleration"])

    def test_get_catalog_with_status(self):
        installed = ["Ministral-3-3B-Instruct-2512-Q4_K_M.gguf"]
        cat = get_catalog_with_status({}, installed)
        found_installed = any(m["is_installed"] for m in cat)
        self.assertTrue(found_installed)

    def test_npu_compatibility(self):
        hw_with_npu = {
            "recommended_backend": "vulkan",
            "gpus": [{"type": "vulkan", "name": "Vulkan Compatible Runtime"}],
            "npus": [{"type": "npu", "name": "Intel(R) AI Boost", "manufacturer": "Intel Corporation"}],
            "cpu": {"count": 14},
        }
        ministral = RECOMMENDED_CATALOG[0]
        self.assertTrue(ministral.get("npu_compatible"))
        res = assess_compatibility(ministral, hw_with_npu)
        self.assertTrue(res["has_npu_acceleration"])
        self.assertEqual(res["color"], "cyan")
        self.assertIn("NPU", res["badge"])

    def test_local_model_detection(self):
        from models_mgr.catalog import detect_local_models
        models = detect_local_models()
        self.assertIsInstance(models, list)
        # Verify detected local models in ai-server/models
        filenames = [m["filename"] for m in models]
        if "Ministral-3-3B-Instruct-2512-Q4_K_M.gguf" in filenames:
            cat = get_catalog_with_status()
            ministral_entry = next((m for m in cat if m["filename"] == "Ministral-3-3B-Instruct-2512-Q4_K_M.gguf"), None)
            self.assertIsNotNone(ministral_entry)
            self.assertTrue(ministral_entry["is_installed"])


class TestHuggingFaceClient(unittest.TestCase):

    def setUp(self):
        from models_mgr.hf_client import HuggingFaceClient
        self.client = HuggingFaceClient(timeout=4)

    def test_hf_search_fallback_models(self):
        # Should return models even without internet access via fallback catalog
        models = self.client.search_models("qwen", limit=10)
        self.assertIsInstance(models, list)
        self.assertGreaterEqual(len(models), 1)
        self.assertTrue(any("qwen" in m["name"].lower() or "qwen" in m["repo"].lower() for m in models))

    def test_hf_evaluate_compatibility_npu(self):
        hw = {
            "ram_total_gib": 16.0,
            "ram_available_gib": 12.0,
            "recommended_backend": "vulkan",
            "gpus": [{"type": "vulkan", "name": "GPU"}],
            "npus": [{"type": "npu", "name": "Intel AI Boost"}],
        }
        res = self.client.evaluate_model_compatibility(size_gib=2.1, model_name="Ministral 3B", hardware_info=hw)
        self.assertTrue(res["is_compatible"])
        self.assertTrue(res["has_npu_acceleration"])
        self.assertEqual(res["color"], "cyan")
        self.assertGreater(res["score"], 80)

    def test_hf_evaluate_compatibility_ram_limit(self):
        # Model requires 45GB RAM on an 8GB machine -> should be marked incompatible
        hw = {
            "ram_total_gib": 8.0,
            "ram_available_gib": 4.0,
            "recommended_backend": "cpu",
            "gpus": [],
            "npus": [],
        }
        res = self.client.evaluate_model_compatibility(size_gib=35.0, model_name="Llama-3.1-70B", hardware_info=hw)
        self.assertFalse(res["is_compatible"])
        self.assertEqual(res["status_tier"], "incompatible")
        self.assertEqual(res["color"], "danger")

    def test_hf_get_all_compatible_models(self):
        hw = {
            "ram_total_gib": 16.0,
            "recommended_backend": "cuda",
            "gpus": [{"type": "cuda", "name": "RTX 4060"}],
            "npus": [],
        }
        models = self.client.get_all_compatible_models(hardware_info=hw, only_compatible=True)
        self.assertIsInstance(models, list)
        self.assertGreaterEqual(len(models), 3)
        for m in models:
            self.assertIn("repo", m)
            self.assertIn("filename", m)
            self.assertIn("compatibility", m)
            self.assertTrue(m["compatibility"]["is_compatible"])

    def test_hf_auto_select_best_models(self):
        hw_npu = {
            "ram_total_gib": 16.0,
            "recommended_backend": "cpu",
            "gpus": [],
            "npus": [{"type": "npu", "name": "Ryzen AI"}],
        }
        best = self.client.auto_select_best_models(hardware_info=hw_npu, top_k=2)
        self.assertGreaterEqual(len(best), 1)
        # Should prioritize NPU-compatible model
        self.assertTrue(best[0]["compatibility"]["has_npu_acceleration"] or best[0]["npu_compatible"])


if __name__ == "__main__":
    unittest.main()

