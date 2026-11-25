"""Short lived approvals for ETU calendar writes in an active chat stream."""
import asyncio
from datetime import datetime
import re
from uuid import uuid4

_pending = {}


async def request(conversation_id, action):
    token = uuid4().hex
    future = asyncio.get_running_loop().create_future()
    _pending[token] = (conversation_id, action, future)
    try:
        return token, future
    finally:
        pass


def decide(token, conversation_id, approved, friend_ids=None, event=None):
    item = _pending.get(token)
    if not item or item[0] != conversation_id or item[2].done():
        return False
    if not isinstance(friend_ids, list):
        friend_ids = [friend_ids] if isinstance(friend_ids, int) and friend_ids > 0 else []
    if len(friend_ids) > 100:
        return False
    selected = list(dict.fromkeys(value for value in friend_ids if isinstance(value, int) and not isinstance(value, bool) and value > 0))[:100]
    if approved and item[1].get("tool") == "etu_schedule_share" and not selected:
        return False
    if approved and item[1].get("tool") == "etu_schedule_create":
        if not isinstance(event, dict) or set(event) != {"title", "start", "end", "location", "description"}:
            return False
        arguments = item[1].get("arguments")
        if not isinstance(arguments, dict):
            return False
        updated = dict(arguments)
        for key, limit in (("title", 200), ("location", 200), ("description", 1000)):
            value = event.get(key, updated.get(key, ""))
            if not isinstance(value, str) or len(value) > limit or key == "title" and not value.strip():
                return False
            updated[key] = value.strip()
        dates = []
        for key in ("start", "end"):
            value = event.get(key, updated.get(key))
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})", value):
                return False
            try:
                dates.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except ValueError:
                return False
            updated[key] = value
        if dates[1] <= dates[0]:
            return False
        item[1]["arguments"] = updated
    _pending.pop(token, None)
    item[2].set_result({"approved": bool(approved), "friend_ids": selected, "arguments": item[1].get("arguments", {})})
    return True


def discard(token):
    _pending.pop(token, None)
