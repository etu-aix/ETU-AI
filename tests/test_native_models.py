import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from models_mgr.downloader import ModelDownloader
from models_mgr.native import BundleTask, NativeInstaller, native_variants, quant_label, safe_path
from storage.db import StorageManager


class Response(io.BytesIO):
    status = 200

    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


class NativeModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.downloader = ModelDownloader(root / "models", StorageManager(root / "models.db"))
        self.installer = NativeInstaller(self.downloader)
        self.contents = {"openvino_model.xml": b"graph", "openvino_model.bin": b"weights", "config.json": b"{}"}
        self.info = {"siblings": [{"rfilename": name, "size": len(content), "lfs": {"oid": hashlib.sha256(content).hexdigest()}}
                                  for name, content in self.contents.items()]}
        self.variant = native_variants("OpenVINO/Qwen-int4", self.info, ("openvino",))[0]

    def tearDown(self):
        self.temp.cleanup()

    def response(self, request, timeout):
        return Response(self.contents[request.full_url.split("/resolve/main/", 1)[1]])

    def task(self):
        return BundleTask("OpenVINO/Qwen-int4", self.variant, self.installer.root / self.variant["id"])

    def test_complete_bundle_installs_config_graph_weights_and_persists(self):
        task = self.task()
        with patch("models_mgr.downloader.open_download", side_effect=self.response):
            self.installer._run(task)
        self.assertEqual(task.status, "completed", task.error_message)
        self.assertEqual(task.to_dict()["percentage"], 100)
        self.assertEqual(task.to_dict()["downloaded_bytes"], sum(map(len, self.contents.values())))
        installed = NativeInstaller(self.downloader).installed()
        self.assertEqual([item["id"] for item in installed], [self.variant["id"]])
        for name, data in self.contents.items():
            self.assertEqual((task.target_dir / name).read_bytes(), data)
        self.installer.remove(self.variant["id"])
        self.assertEqual(self.installer.installed(), [])

    def test_checksum_failure_never_marks_bundle_installed(self):
        task = self.task()
        with patch("models_mgr.downloader.open_download", return_value=Response(b"invalid")):
            self.installer._run(task)
        self.assertEqual(task.status, "error")
        self.assertEqual(self.installer.installed(), [])

    def test_cancelled_bundle_can_resume_verified_files(self):
        task = self.task()
        task.cancel_event.set()
        self.installer._run(task)
        self.assertEqual(task.status, "cancelled")
        self.assertEqual(self.installer.installed(), [])
        task = self.task()
        with patch("models_mgr.downloader.open_download", side_effect=self.response):
            self.installer._run(task)
        self.assertEqual(task.status, "completed")
        resumed = self.task()
        with patch("models_mgr.downloader.open_download") as download:
            self.installer._run(resumed)
        download.assert_not_called()
        self.assertEqual(resumed.status, "completed")

    def test_precision_variants_only_include_own_weights_and_shared_tokenizer(self):
        names = ["int4/openvino_model.xml", "int4/openvino_model.bin", "fp16/openvino_model.xml",
                 "fp16/openvino_model.bin", "openvino_tokenizer.xml", "openvino_tokenizer.bin", "config.json", "model.gguf", "script.py"]
        variants = native_variants("owner/model", {"siblings": [{"rfilename": name} for name in names]}, ("openvino",))
        self.assertEqual({item["quant"] for item in variants}, {"INT4", "FP16"})
        int4 = next(item for item in variants if item["quant"] == "INT4")
        self.assertEqual({item["filename"] for item in int4["files"]}, {
            "int4/openvino_model.xml", "int4/openvino_model.bin", "openvino_tokenizer.xml", "openvino_tokenizer.bin", "config.json"})

    def test_onnx_variants_keep_their_external_weights(self):
        names = ["onnx/model_q4.onnx", "onnx/model_q4.onnx_data", "onnx/model_fp16.onnx",
                 "onnx/model_fp16.onnx_data", "tokenizer.json"]
        variants = native_variants("owner/model", {"siblings": [{"rfilename": name} for name in names]}, ("onnx",))
        q4 = next(item for item in variants if item["quant"] == "INT4")
        self.assertEqual({item["filename"] for item in q4["files"]}, {"onnx/model_q4.onnx", "onnx/model_q4.onnx_data", "tokenizer.json"})

    def test_split_model_graphs_form_one_complete_variant(self):
        names = ["openvino_model.xml", "openvino_model.bin", "openvino_vision_embeddings_model.xml",
                 "openvino_vision_embeddings_model.bin", "chat_template.jinja"]
        variants = native_variants("owner/model-int4", {"siblings": [{"rfilename": name} for name in names]}, ("openvino",))
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0]["filename"], "openvino_model.xml")
        self.assertEqual({item["filename"] for item in variants[0]["files"]}, set(names))

    def test_rejects_traversal_and_windows_paths(self):
        for name in ("../model.bin", "/model.bin", "C:/model.bin", "a\\b.bin", "a/../b.bin", "NUL.bin", "a/file."):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_path(name)
        with self.assertRaises(ValueError):
            self.installer.remove("../outside")

    def test_partial_bundle_is_not_reported_as_installed(self):
        task = self.task()
        with patch("models_mgr.downloader.open_download", side_effect=self.response):
            self.installer._run(task)
        (task.target_dir / "openvino_model.bin").unlink()
        self.assertEqual(self.installer.installed(), [])

    def test_quant_labels_do_not_invent_precision(self):
        self.assertEqual(quant_label("owner/model-int4-symg128-ov"), "INT4")
        self.assertEqual(quant_label("onnx/model_fp16.onnx"), "FP16")
        self.assertEqual(quant_label("owner/model"), "Précision non indiquée")


if __name__ == "__main__":
    unittest.main()
