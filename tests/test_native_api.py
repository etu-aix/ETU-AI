"""Exercise the install -> progress -> library -> uninstall API flow."""
import hashlib
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import api_server
from models_mgr.downloader import ModelDownloader
from models_mgr.native import NativeInstaller, native_variants


class NativeAPITests(unittest.TestCase):
    def test_installs_selected_precision_and_exposes_it_in_library(self):
        with tempfile.TemporaryDirectory() as folder:
            downloader = ModelDownloader(Path(folder) / "models", Mock())
            installer = NativeInstaller(downloader)
            contents = {"int4/openvino_model.xml": b"graph", "int4/openvino_model.bin": b"weights", "config.json": b"{}"}
            info = {"siblings": [{"rfilename": name, "size": len(data), "lfs": {"oid": hashlib.sha256(data).hexdigest()}}
                                 for name, data in contents.items()]}
            variants = native_variants("owner/model", info, ("openvino",))
            completed = threading.Event()
            run = installer._run

            def finish(task):
                try:
                    run(task)
                finally:
                    completed.set()

            def response(request, timeout):
                data = contents[request.full_url.split("/resolve/main/", 1)[1]]
                stream = io.BytesIO(data)
                stream.status = 200
                stream.headers = {"Content-Length": str(len(data))}
                return stream

            with patch.object(api_server, "downloader", downloader), patch.object(api_server, "native_installer", installer), \
                 patch.object(api_server.hf_client, "get_native_variants", return_value=variants), \
                 patch.object(installer, "_run", side_effect=finish), \
                 patch("models_mgr.downloader.open_download", side_effect=response), \
                 patch.object(api_server, "get_hardware_info", return_value={}), \
                 patch.object(api_server, "get_catalog_with_status", return_value=[]), \
                 patch.object(api_server, "get_configured_api_models", return_value=[]):
                client = TestClient(api_server.app)
                try:
                    request = {"repo": "owner/model", "filename": "int4/openvino_model.xml", "model_format": "openvino"}
                    result = client.post("/api/models/native/download", json=request)
                    self.assertEqual(result.status_code, 200, result.text)
                    self.assertTrue(completed.wait(5), "Installation did not finish")
                    task_id = result.json()["task_id"]
                    progress = client.get(f"/api/models/tasks/{task_id}").json()
                    self.assertEqual(progress["status"], "completed", progress)
                    self.assertEqual(progress["percentage"], 100)
                    catalog = client.get("/api/models/catalog").json()
                    self.assertEqual(catalog["native_models"][0]["quant"], "INT4")
                    self.assertEqual(client.post("/api/models/native/download", json=request).status_code, 409)
                    self.assertEqual(client.delete(f'/api/models/native/{variants[0]["id"]}').status_code, 200)
                    self.assertEqual(client.get("/api/models/catalog").json()["native_models"], [])
                finally:
                    client.close()


if __name__ == "__main__":
    unittest.main()
