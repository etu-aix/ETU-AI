"""Tests unitaires pour le routeur d'outils etu-aix."""
import unittest
from pathlib import Path
import sys

# Ajout du dossier parent au sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.executor import ToolExecutor


class TestToolExecutor(unittest.TestCase):

    def setUp(self):
        self.executor = ToolExecutor()

    def test_schema_loaded(self):
        tools = self.executor.get_openai_tools()
        self.assertIsInstance(tools, list)
        self.assertGreaterEqual(len(tools), 5)
        names = [t["function"]["name"] for t in tools]
        self.assertIn("get_timetable", names)
        self.assertIn("create_calendar_event", names)
        self.assertIn("send_friend_request", names)
        self.assertIn("review_document", names)

    def test_get_timetable_mock(self):
        res = self.executor.execute_tool("get_timetable", {"target_date": "today", "view": "day"})
        self.assertTrue(res["success"])
        self.assertIn("courses", res["data"])
        self.assertGreaterEqual(len(res["data"]["courses"]), 1)

    def test_create_event_mock(self):
        res = self.executor.execute_tool(
            "create_calendar_event",
            {"title": "Révision IA", "start_time": "2026-10-01T14:00:00", "location": "Salle 102"},
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["data"]["status"], "created")

    def test_send_friend_request_mock(self):
        res = self.executor.execute_tool(
            "send_friend_request",
            {"student_query": "Alexandre", "message": "Salut !"},
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["data"]["status"], "pending")

    def test_unknown_tool(self):
        res = self.executor.execute_tool("non_existent_tool", {})
        self.assertFalse(res["success"])
        self.assertIn("non reconnu", res["error"])


if __name__ == "__main__":
    unittest.main()
