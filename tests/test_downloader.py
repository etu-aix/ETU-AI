import hashlib
import io
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from models_mgr.downloader import DownloadTask, ModelDownloader, _connect_download
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
        with patch("models_mgr.downloader.open_download", side_effect=fake_open):
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
        with patch("models_mgr.downloader.open_download", return_value=FakeResponse(b"model", 200, {"Content-Length": "5"})):
            self.downloader._run_download(task)
        self.assertEqual(task.status, "error")
        self.assertFalse((self.models / "small.gguf").exists())

    def test_cancel_keeps_partial_file_for_resume(self):
        task = DownloadTask("task", "owner/model", "small.gguf", self.models)
        task.cancel_event.set()
        with patch("models_mgr.downloader.open_download", return_value=FakeResponse(b"model", 200, {"Content-Length": "5"})):
            self.downloader._run_download(task)
        self.assertEqual(task.status, "cancelled")
        self.assertEqual(len(list(self.models.glob("*.part"))), 1)

    def test_download_prefers_ipv4_over_unreachable_ipv6(self):
        addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', 443, 0, 0)),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        connection = MagicMock()
        with patch('socket.getaddrinfo', return_value=addresses), patch('socket.socket', return_value=connection) as factory:
            self.assertIs(_connect_download(('example.com', 443), timeout=30), connection)
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM, 6)
        connection.connect.assert_called_once_with(('127.0.0.1', 443))

    def test_unexpected_partial_response_cannot_install_truncated_model(self):
        task = DownloadTask('task', 'owner/model', 'small.gguf', self.models)
        with patch('models_mgr.downloader.open_download', return_value=FakeResponse(b'part', 206, {'Content-Length': '4', 'Content-Range': 'bytes 0-3/100'})):
            self.downloader._run_download(task)
        self.assertEqual(task.status, 'error')
        self.assertFalse((self.models / 'small.gguf').exists())

    def test_download_falls_back_to_ipv6_if_ipv4_fails(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443)),
                     (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', 443, 0, 0))]
        ipv4, ipv6 = MagicMock(), MagicMock()
        ipv4.connect.side_effect = OSError('unreachable')
        with patch('socket.getaddrinfo', return_value=addresses), patch('socket.socket', side_effect=[ipv4, ipv6]):
            self.assertIs(_connect_download(('example.com', 443), timeout=30), ipv6)
        ipv4.close.assert_called_once()
        ipv6.connect.assert_called_once_with(('::1', 443, 0, 0))


if __name__ == "__main__":
    unittest.main()
