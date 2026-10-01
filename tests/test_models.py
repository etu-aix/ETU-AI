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


if __name__ == "__main__":
    unittest.main()
