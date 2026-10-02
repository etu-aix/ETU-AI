import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from models_mgr.downloader import DownloadTask, ModelDownloader
from storage.db import StorageManager


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, status: int, headers: dict[str, str]):
        super().__init__(data)
        self.status = status
        self.headers = headers


class TestModelDownloader(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.models = root / "models"
        self.downloader = ModelDownloader(self.models, StorageManager(root / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_resumes_partial_file_and_records_full_digest(self):
        repo, filename = "owner/model", "small.gguf"
        suffix = hashlib.sha256(repo.encode()).hexdigest()[:12]
        partial = self.models / f"{filename}.{suffix}.part"
        partial.write_bytes(b"first-")
        task = DownloadTask("task", repo, filename, self.models)
        def fake_open(request, timeout):
            self.assertEqual(request.get_header("Range"), "bytes=6-")
            return FakeResponse(b"second", 206, {"Content-Range": "bytes 6-11/12", "Content-Length": "6"})
        with patch("urllib.request.urlopen", side_effect=fake_open):
            self.downloader._run_download(task)
        self.assertEqual(task.status, "completed")
        self.assertEqual((self.models / filename).read_bytes(), b"first-second")
        self.assertEqual(task.sha256, hashlib.sha256(b"first-second").hexdigest())
        self.assertFalse(partial.exists())

    def test_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            self.downloader.start_download("owner/model", "../outside.gguf")

    def test_rejects_bad_published_checksum(self):
        task = DownloadTask("task", "owner/model", "small.gguf", self.models, "0" * 64)
        with patch("urllib.request.urlopen", return_value=FakeResponse(b"model", 200, {"Content-Length": "5"})):
            self.downloader._run_download(task)
        self.assertEqual(task.status, "error")
        self.assertFalse((self.models / "small.gguf").exists())

    def test_cancel_keeps_partial_file_for_resume(self):
        task = DownloadTask("task", "owner/model", "small.gguf", self.models)
        task.cancel_event.set()
        with patch("urllib.request.urlopen", return_value=FakeResponse(b"model", 200, {"Content-Length": "5"})):
            self.downloader._run_download(task)
        self.assertEqual(task.status, "cancelled")
        self.assertEqual(len(list(self.models.glob("*.part"))), 1)


if __name__ == "__main__":
    unittest.main()
