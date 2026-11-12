"""Short lived approvals for ETU calendar writes in an active chat stream."""
import asyncio
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


def decide(token, conversation_id, approved, friend_id=None):
    item = _pending.pop(token, None)
    if not item or item[0] != conversation_id or item[2].done():
        return False
    item[2].set_result({"approved": bool(approved), "friend_id": friend_id if isinstance(friend_id, int) and friend_id > 0 else None})
    return True


def discard(token):
    _pending.pop(token, None)
