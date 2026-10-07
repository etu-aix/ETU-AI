"""Exact projector matching, file isolation, import and confirmed Beacon loading."""

from io import BytesIO
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import api_server
from models_mgr.catalog import detect_local_models
from models_mgr.downloader import DownloadTask, ModelDownloader
from models_mgr.projectors import ProjectorManager, matching_projectors, model_identity, projector_alias
from storage.db import StorageManager

MODEL = "Ministral-3-3B-Instruct-2512-Q4_K_M.gguf"
PROJECTOR = "Ministral-3-3B-Instruct-2512-BF16-mmproj.gguf"
REPO = "mistralai/Ministral-3-3B-Instruct-2512-GGUF"


def entry(filename):
    return {"filename": filename, "size_bytes": 100, "size_gib": 0.1, "sha256": "a" * 64}


class TestProjectorMatching(unittest.TestCase):
    def test_official_ministral_exact_match_and_wrong_size_rejected(self):
        files = [entry(MODEL), entry(PROJECTOR), entry(PROJECTOR.replace("3B", "8B"))]
        self.assertEqual([item["filename"] for item in matching_projectors(MODEL, files)], [PROJECTOR])
        self.assertEqual(model_identity(MODEL), model_identity(PROJECTOR))

    def test_generic_projector_requires_single_variant(self):
        model = "Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf"
        generic = entry("mmproj-F16.gguf")
        self.assertEqual(matching_projectors(model, [entry(model), entry(model.replace("Q4_K_M", "Q8_0")), generic]), [generic])
        self.assertEqual(matching_projectors(model, [entry(model), entry(model.replace("3B", "7B")), generic]), [])

    def test_family_name_alone_does_not_match(self):
        model = "Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf"
        self.assertEqual(matching_projectors(model, [entry(model), entry("mmproj-Qwen2.5-VL-7B-Instruct-f16.gguf")]), [])

    def test_text_model_cannot_gain_vision_from_projector(self):
        model = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
        self.assertEqual(matching_projectors(model, [entry(model), entry("mmproj-F16.gguf")]), [])
        local_text_model = "hyperclovax-seed-text-instruct-1.5b-q4_k_m.gguf"
        self.assertEqual(matching_projectors(local_text_model, [entry(local_text_model), entry("mmproj-F16.gguf")]), [])

    def test_projectors_are_not_language_models(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            storage = StorageManager(folder / "chat.db")
            for name in (MODEL, PROJECTOR):
                with (folder / name).open("wb") as output:
                    output.truncate(2 * 1024 * 1024)
            downloader = ModelDownloader(folder, storage)
            self.assertEqual(downloader.list_installed_models(), [MODEL])
            self.assertEqual(downloader.list_installed_projectors(), [PROJECTOR])
            self.assertEqual([item["filename"] for item in detect_local_models(folder)], [MODEL])

    def test_generic_names_from_different_repos_are_isolated(self):
        self.assertNotEqual(projector_alias("owner/model-3B", "mmproj-F16.gguf"), projector_alias("owner/model-7B", "mmproj-F16.gguf"))

    def test_download_uses_original_url_and_namespaced_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            storage = StorageManager(folder / "chat.db")
            downloader = ModelDownloader(folder, storage)
            data = b"projector contents"
            alias = projector_alias(REPO, PROJECTOR)
            task = DownloadTask("task", REPO, PROJECTOR, folder, hashlib.sha256(data).hexdigest(), alias)
            response = BytesIO(data)
            response.headers = {"Content-Length": str(len(data))}
            response.status = 200
            with patch("urllib.request.urlopen", return_value=response) as request:
                downloader._run_download(task)
            self.assertEqual(task.status, "completed")
            self.assertTrue(request.call_args.args[0].full_url.endswith("/" + PROJECTOR))
            self.assertEqual((folder / alias).read_bytes(), data)
            self.assertFalse((folder / PROJECTOR).exists())
            self.assertEqual(storage.list_cached_models()[0]["filename"], alias)


class TestProjectorAPI(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        self.storage = StorageManager(self.folder / "chat.db")
        self.downloader = ModelDownloader(self.folder, self.storage)
        (self.folder / MODEL).write_bytes(b"GGUF")
        self.hf = MagicMock()
        self.hf.get_model_files.return_value = [entry(MODEL), entry(PROJECTOR)]
        self.patches = [patch.object(api_server, "storage", self.storage), patch.object(api_server, "downloader", self.downloader), patch.object(api_server, "hf_client", self.hf)]
        for item in self.patches:
            item.start()
        self.client = TestClient(api_server.app)

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def test_discovery_and_download_assign_correct_file(self):
        result = self.client.get("/api/models/mmproj", params={"model": MODEL}).json()
        self.assertEqual(result["candidates"][0]["filename"], PROJECTOR)
        with patch.object(self.downloader, "start_download", return_value="task") as download:
            response = self.client.post("/api/models/mmproj/download", json={"model": MODEL, "repo": REPO, "filename": PROJECTOR})
        self.assertEqual(response.status_code, 200)
        alias = projector_alias(REPO, PROJECTOR)
        download.assert_called_once_with(REPO, PROJECTOR, "a" * 64, local_filename=alias)
        self.assertEqual(response.json()["association"]["local_filename"], alias)
        self.assertEqual(json.loads((self.folder / ".etu-mmproj.json").read_text())[MODEL]["local_filename"], alias)

    def test_wrong_projector_and_uninstalled_model_do_not_start_download(self):
        with patch.object(self.downloader, "start_download") as download:
            for model, projector in [(MODEL, PROJECTOR.replace("3B", "8B")), (MODEL.replace("3B", "8B"), PROJECTOR)]:
                response = self.client.post("/api/models/mmproj/download", json={"model": model, "repo": REPO, "filename": projector})
                self.assertEqual(response.status_code, 422)
            download.assert_not_called()

    def test_missing_associated_projector_prevents_activation(self):
        manager = ProjectorManager(self.storage, self.downloader, self.hf)
        manager.assign(MODEL, {"local_filename": projector_alias(REPO, PROJECTOR), "filename": PROJECTOR})
        with patch("urllib.request.urlopen") as request:
            self.assertEqual(self.client.post("/api/models/select", json={"model": MODEL}).status_code, 409)
            request.assert_not_called()

    def test_local_import_is_persistent_and_invalid_file_is_cleaned_up(self):
        params = {"model": MODEL, "filename": "mmproj-F16.gguf"}
        invalid = self.client.post("/api/models/mmproj/import", params=params, content=b"not GGUF")
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(list(self.folder.glob("*.part")), [])
        valid = b"GGUF" + struct.pack("<IQQ", 3, 1, 1) + b"payload"
        response = self.client.post("/api/models/mmproj/import", params=params, content=valid)
        self.assertEqual(response.status_code, 200)
        association = response.json()["association"]
        self.assertTrue(association["manual"])
        self.assertEqual((self.folder / association["local_filename"]).read_bytes(), valid)
        self.assertEqual(self.client.delete("/api/models/mmproj", params={"model": MODEL}).status_code, 200)
        self.assertIsNone(ProjectorManager(self.storage, self.downloader, self.hf).association(MODEL))

    def test_same_model_is_reloaded_when_projector_changes(self):
        alias = projector_alias(REPO, PROJECTOR)
        (self.folder / alias).write_bytes(b"GGUF")
        ProjectorManager(self.storage, self.downloader, self.hf).assign(MODEL, {"local_filename": alias, "filename": PROJECTOR})
        selected = []
        stopped = []
        states = iter([{"status": "running", "active_model": Path(MODEL).stem, "active_mmproj": None}, {"status": "running", "active_model": Path(MODEL).stem, "active_mmproj": alias}])

        def beacon(request, **kwargs):
            url = request if isinstance(request, str) else request.full_url
            if url.endswith("/api/status"):
                data = next(states)
            elif url.endswith("/api/stop"):
                stopped.append(True)
                data = {}
            else:
                selected.append(json.loads(request.data))
                data = {"success": True}
            return BytesIO(json.dumps(data).encode())

        with patch("urllib.request.urlopen", side_effect=beacon):
            response = self.client.post("/api/models/select", json={"model": MODEL})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(stopped, [True])
        self.assertEqual(selected[0]["mmproj"], alias)
        self.assertEqual(response.json()["active_mmproj"], alias)
