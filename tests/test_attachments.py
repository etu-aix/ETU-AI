"""File extraction, provider payloads and persistence across conversation turns."""

import base64
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from fastapi.testclient import TestClient

import api_server
from chat.attachments import prepare_attachments, load_attachments, message_content, gemini_parts, save_attachments
from chat.sse_handler import ChatStreamHandler
from storage.db import StorageManager
from mock_sse import mock_sse


def file(name, data):
    return SimpleNamespace(name=name, data=base64.b64encode(data).decode())


def png():
    output = BytesIO()
    Image.new("RGB", (20, 20), "red").save(output, format="PNG")
    return output.getvalue()


class TestAttachments(unittest.TestCase):
    def test_text_and_encoding(self):
        items = prepare_attachments([file("cours.txt", "Résumé SQL".encode()), file("notes.csv", "élève;note".encode("cp1252"))])
        self.assertEqual(items[0]["text"], "Résumé SQL")
        self.assertIn("élève", items[1]["text"])

    def test_pdf_text(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 10 100 Td (Cours SQL) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        output = BytesIO()
        writer.write(output)
        self.assertIn("Cours SQL", prepare_attachments([file("cours.pdf", output.getvalue())])[0]["text"])

    def test_scanned_pdf_explains_limit(self):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        output = BytesIO()
        writer.write(output)
        with self.assertRaisesRegex(ValueError, "PDF scanné"):
            prepare_attachments([file("scan.pdf", output.getvalue())])

    def test_office_documents(self):
        for name, members, expected in [
            ("cours.docx", {"word/document.xml": '<w:document xmlns:w="urn:w"><w:p><w:r><w:t>SQL</w:t></w:r></w:p></w:document>'}, "SQL"),
            ("notes.xlsx", {"xl/sharedStrings.xml": '<sst xmlns="urn:x"><si><t>Alice</t></si></sst>', "xl/worksheets/sheet1.xml": '<sheet xmlns="urn:x"><row><c r="A1" t="s"><v>0</v></c><c r="B1"><f>1+2</f><v>3</v></c></row></sheet>'}, "A1: Alice\tB1: 3 [formule: 1+2]"),
        ]:
            with self.subTest(name=name):
                output = BytesIO()
                with ZipFile(output, "w") as archive:
                    for path, text in members.items():
                        archive.writestr(path, text)
                self.assertIn(expected, prepare_attachments([file(name, output.getvalue())])[0]["text"])

    def test_invalid_files_and_limits(self):
        for entry in [file("bad.exe", b"MZ"), file("bad.pdf", b"fake"), file("bad.png", b"fake"), file("empty.txt", b""), file("binary.txt", b"\x00abc"), SimpleNamespace(name="a.txt", data="!!")]:
            with self.subTest(name=entry.name), self.assertRaises(ValueError):
                prepare_attachments([entry])
        with self.assertRaisesRegex(ValueError, "6 fichiers"):
            prepare_attachments([file("a.txt", b"a")] * 7)
        with self.assertRaisesRegex(ValueError, "10 Mo"):
            prepare_attachments([file("big.txt", b"x" * (10 * 1024 * 1024 + 1))])

    def test_truncation_is_visible(self):
        item = prepare_attachments([file("long.txt", b"a" * 40000)])[0]
        self.assertEqual(len(item["text"]), 30000)
        self.assertIn("30 000", item["warning"])

    def test_images_for_all_providers(self):
        attachments = prepare_attachments([file("photo.png", png())])
        content = message_content("Analyse", attachments)
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(message_content("Analyse", attachments, "anthropic")[1]["source"]["media_type"], "image/jpeg")
        self.assertEqual(gemini_parts(content)[1]["inlineData"]["mimeType"], "image/jpeg")

    def test_delete_cleans_attachments(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory) / "chat.db")
            conv = storage.create_conversation()
            message = storage.add_message(conv, "user", "Bonjour")
            save_attachments(storage, message, prepare_attachments([file("notes.txt", b"SQL")]))
            storage.delete_conversation(conv)
            with storage._connection() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0], 0)


class TestAttachmentStreaming(unittest.IsolatedAsyncioTestCase):
    async def test_remote_payloads_include_images_and_document_text(self):
        for provider_type in ("openai", "anthropic", "gemini"):
            with self.subTest(provider=provider_type), tempfile.TemporaryDirectory() as directory:
                storage = StorageManager(Path(directory) / "chat.db")
                storage.set_setting("api_provider", {"enabled": True, "provider": provider_type, "type": provider_type, "model": "vision", "base_url": "https://example.invalid"})
                conv = storage.create_conversation()
                handler = ChatStreamHandler(storage=storage)
                with patch("chat.sse_handler.get_api_key", return_value="test"), mock_sse() as requests:
                    async for _ in handler.stream_chat(conv, "Analyse", attachments=prepare_attachments([file("cours.txt", b"SELECT SQL"), file("photo.png", png())])):
                        pass
                payload = json.loads(requests[0].content)
                if provider_type == "gemini":
                    parts = payload["contents"][0]["parts"]
                    self.assertIn("SELECT SQL", parts[0]["text"])
                    self.assertIn("inlineData", parts[1])
                else:
                    parts = payload["messages"][-1]["content"]
                    self.assertIn("SELECT SQL", parts[0]["text"])
                    self.assertEqual(parts[1]["type"], "image" if provider_type == "anthropic" else "image_url")

    async def test_attachments_survive_followup_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat.db"
            storage = StorageManager(path)
            conv = storage.create_conversation()
            handler = ChatStreamHandler(storage=storage)
            attachments = prepare_attachments([file("cours.txt", b"SELECT important"), file("photo.png", png())])
            with mock_sse() as requests:
                async for _ in handler.stream_chat(conv, "Analyse", attachments=attachments):
                    pass
                first = json.loads(requests[-1].content)
                self.assertIn("SELECT important", first["messages"][-1]["content"][0]["text"])
                async for _ in handler.stream_chat(conv, "Explique le document"):
                    pass
                second = json.loads(requests[-1].content)
                self.assertIsInstance(second["messages"][1]["content"], list)
            reopened = StorageManager(path)
            self.assertEqual(len(load_attachments(reopened, reopened.get_messages(conv))[0]["attachments"]), 2)


class TestAttachmentAPI(unittest.TestCase):
    def test_file_only_chat_and_validation_before_conversation_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory) / "chat.db")
            wizard = MagicMock()
            wizard.is_completed.return_value = False
            with patch.object(api_server, "storage", storage), patch.object(api_server, "state_manager", MagicMock()), patch.object(api_server, "wizard_manager", wizard), patch.object(api_server.stream_handler, "storage", storage), mock_sse(), TestClient(api_server.app) as client:
                bad = client.post("/api/chat/stream", json={"attachments": [vars(file("bad.exe", b"MZ"))]})
                self.assertEqual(bad.status_code, 422)
                self.assertEqual(storage.list_conversations(), [])
                self.assertEqual(client.post("/api/chat/stream", json={}).status_code, 422)
                response = client.post("/api/chat/stream", json={"attachments": [vars(file("cours.txt", b"SELECT"))]})
                self.assertEqual(response.status_code, 200)
                conv = response.headers["X-Conversation-Id"]
                history = client.get(f"/api/conversations/{conv}/messages").json()
                self.assertEqual(history["messages"][0]["attachments"][0]["name"], "cours.txt")
                self.assertNotIn("text", history["messages"][0]["attachments"][0])
