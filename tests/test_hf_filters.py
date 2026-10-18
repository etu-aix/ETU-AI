"""Explorer filters use parameter counts and the repository's actual publisher."""
import unittest
from unittest.mock import patch

from models_mgr.hf_client import HuggingFaceClient, TRUSTED_GGUF_PUBLISHERS


def model(repo, params, size_gib):
    return {
        "repo": repo, "name": repo.split("/", 1)[1], "params": params,
        "default_file": {"filename": "model.gguf", "size_gib": size_gib, "quant": "Q4_K_M"},
    }


class HuggingFaceFilterTests(unittest.TestCase):
    def setUp(self):
        self.client = HuggingFaceClient()

    def test_range_boundaries_have_no_overlap(self):
        cases = [("1B", "1to2"), ("1.5B", "1to2"), ("2B", "2to4"),
                 ("4B", "4to7"), ("7B", "7to14"), ("14B", "14to35"),
                 ("34.9B", "14to35"), ("35B", "35plus"), ("70B", "35plus")]
        ranges = ("1to2", "2to4", "4to7", "7to14", "14to35", "35plus")
        for label, expected in cases:
            with self.subTest(label=label):
                self.assertEqual([key for key in ranges if self.client._matches_parameter_range(label, key)], [expected])
        self.assertFalse(self.client._matches_parameter_range("Inconnu", "1to2"))

    def test_filter_uses_parameters_independently_of_download_size(self):
        candidates = [model("Qwen/Small-1.5B-GGUF", "1.5B", 20), model("Qwen/Large-35B-GGUF", "35B", 1)]
        with patch.object(self.client, "search_models", return_value=candidates):
            small = self.client.get_all_compatible_models(parameter_range="1to2", only_compatible=False, preserve_order=True)
            large = self.client.get_all_compatible_models(parameter_range="35plus", only_compatible=False, preserve_order=True)
        self.assertEqual([item["params"] for item in small], ["1.5B"])
        self.assertEqual([item["params"] for item in large], ["35B"])

    def test_trusted_filter_checks_author_rather_than_model_name(self):
        candidates = [model("Qwen/Official-7B", "7B", 4), model("community/Qwen-7B", "7B", 4)]
        with patch.object(self.client, "search_models", return_value=candidates) as search:
            results = self.client.get_all_compatible_models(trusted_only=True, only_compatible=False, preserve_order=True)
        self.assertEqual([item["repo"] for item in results], ["Qwen/Official-7B"])
        self.assertEqual({call.kwargs["author"] for call in search.call_args_list}, set(TRUSTED_GGUF_PUBLISHERS))

    def test_excluded_parameter_sizes_do_not_fetch_files(self):
        with patch.object(self.client, "search_models", return_value=[{"repo": "Qwen/Model-70B", "params": "70B"}]), patch.object(self.client, "get_model_files") as files:
            results = self.client.get_all_compatible_models(parameter_range="1to2", preserve_order=True)
        self.assertEqual(results, [])
        files.assert_not_called()

    def test_offline_author_search_keeps_only_that_publisher(self):
        with patch.object(self.client, "_get_json", return_value=None):
            results = self.client.search_models(author="Qwen", single_source=True)
        self.assertTrue(results)
        self.assertTrue(all(item["repo"].startswith("Qwen/") for item in results))


if __name__ == "__main__":
    unittest.main()
