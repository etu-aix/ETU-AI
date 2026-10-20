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

    def test_native_search_checks_files_without_gguf_fallback(self):
        requests = []

        def response(url):
            requests.append(url)
            if "/models?" in url:
                return [{"id": "OpenVINO/Qwen-2B", "downloads": 10}, {"id": "community/Empty-2B"}]
            if "OpenVINO/Qwen-2B?" in url:
                return {"siblings": [{"rfilename": "openvino_model.xml"}, {"rfilename": "openvino_model.bin", "size": 1024}]}
            return {"siblings": [{"rfilename": "model.gguf"}]}

        with patch.object(self.client, "_get_json", side_effect=response):
            results = self.client.search_npu_models()
        self.assertEqual([item["repo"] for item in results], ["OpenVINO/Qwen-2B"])
        self.assertTrue(results[0]["can_install"])
        self.assertTrue(results[0]["quantizations"])
        self.assertFalse(results[0]["can_run"])
        self.assertFalse(results[0]["compatibility"]["npu_compatible"])
        self.assertTrue(all("gguf" not in url for url in requests))

    def test_native_ir_requires_matching_weights(self):
        with patch.object(self.client, "_get_json", side_effect=[
            [{"id": "OpenVINO/Qwen-2B"}],
            {"siblings": [{"rfilename": "openvino_model.xml"}]},
        ]):
            self.assertEqual(self.client.search_npu_models(model_format="openvino"), [])

    def test_gguf_with_only_openvino_tokenizer_is_not_an_ir_model(self):
        with patch.object(self.client, "_get_json", side_effect=[
            [{"id": "community/Ministral-3B-GGUF"}],
            {"siblings": [{"rfilename": name} for name in (
                "Ministral-3B.gguf", "openvino_tokenizer.xml", "openvino_tokenizer.bin",
                "openvino_detokenizer.xml", "openvino_detokenizer.bin",
            )]},
        ]):
            self.assertEqual(self.client.search_npu_models(model_format="openvino"), [])

    def test_native_onnx_supports_subdirectories_and_trusted_publishers(self):
        with patch.object(self.client, "_get_json", side_effect=[
            [{"id": "microsoft/Phi-4B"}, {"id": "community/Phi-4B"}],
            {"siblings": [{"rfilename": "onnx/model.onnx", "size": 100}]},
        ]):
            results = self.client.search_npu_models(model_format="onnx", trusted_only=True, parameter_range="4to7")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["filename"], "onnx/model.onnx")

    def test_native_formats_are_excluded_from_runnable_only_results(self):
        with patch.object(self.client, "_get_json") as request:
            self.assertEqual(self.client.search_npu_models(only_compatible=True), [])
        request.assert_not_called()

    def test_native_precision_filters_actual_variants(self):
        def response(url):
            if "/models?" in url:
                return [{"id": "OpenVINO/Qwen-2B"}]
            return {"siblings": [{"rfilename": name} for name in (
                "int4/openvino_model.xml", "int4/openvino_model.bin",
                "fp16/openvino_model.xml", "fp16/openvino_model.bin",
            )]}
        with patch.object(self.client, "_get_json", side_effect=response):
            results = self.client.search_npu_models(model_format="openvino", precision="int4")
        self.assertEqual([variant["quant"] for variant in results[0]["quantizations"]], ["INT4"])

    def test_native_search_offline_never_returns_gguf(self):
        with patch.object(self.client, "_get_json", return_value=None):
            self.assertEqual(self.client.search_npu_models(), [])

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
