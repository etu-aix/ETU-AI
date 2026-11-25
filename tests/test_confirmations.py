import asyncio
import unittest

from chat.confirmations import decide, request


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_share_confirmation_keeps_unique_selected_friend_ids(self):
        action = {"tool": "etu_schedule_share", "arguments": {"event_id": 5}}
        token, decision = await request("conversation", action)
        self.assertTrue(decide(token, "conversation", True, [12, 12, 0, "13", 18]))
        self.assertEqual({"approved": True, "friend_ids": [12, 18], "arguments": {"event_id": 5}}, await decision)

    async def test_confirmation_is_bound_to_its_conversation(self):
        token, decision = await request("original", {"tool": "etu_schedule_create"})
        self.assertFalse(decide(token, "other", True, []))
        self.assertTrue(decide(token, "original", False, []))
        self.assertEqual({"approved": False, "friend_ids": [], "arguments": {}}, await decision)

    async def test_share_confirmation_requires_recipients_and_caps_selection(self):
        token, decision = await request("conversation", {"tool": "etu_schedule_share"})
        self.assertFalse(decide(token, "conversation", True, []))
        self.assertFalse(decide(token, "conversation", True, list(range(1, 102))))
        self.assertTrue(decide(token, "conversation", True, [42]))
        self.assertEqual({"approved": True, "friend_ids": [42], "arguments": {}}, await decision)

    async def test_create_confirmation_applies_edited_fields(self):
        action = {"tool": "etu_schedule_create", "arguments": {"title": "Pause", "start": "2026-11-18T10:00:00+01:00", "end": "2026-11-18T10:30:00+01:00", "location": "", "description": "", "confirm": True}}
        token, decision = await request("conversation", action)
        edited = {"title": "Révision", "start": "2026-11-19T11:00:00+01:00", "end": "2026-11-19T12:00:00+01:00", "location": "BU", "description": "Apporter les notes"}
        self.assertTrue(decide(token, "conversation", True, [12], edited))
        choice = await decision
        self.assertEqual({**edited, "confirm": True}, choice["arguments"])
        self.assertEqual([12], choice["friend_ids"])

    async def test_create_confirmation_rejects_invalid_or_reversed_times(self):
        action = {"tool": "etu_schedule_create", "arguments": {"title": "Pause", "start": "2026-11-18T10:00:00+01:00", "end": "2026-11-18T10:30:00+01:00"}}
        token, decision = await request("conversation", action)
        self.assertFalse(decide(token, "conversation", True, [], {"title": "Pause", "start": "2026-11-18T12:00:00+01:00", "end": "2026-11-18T11:00:00+01:00"}))
        self.assertFalse(decision.done())
        self.assertTrue(decide(token, "conversation", False, []))
        await decision


if __name__ == "__main__":
    unittest.main()
