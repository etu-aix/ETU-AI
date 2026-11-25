"""Short-lived snapshots of quota values explicitly reported by providers."""

from __future__ import annotations

import re
import threading
import time
from typing import Any


_lock = threading.Lock()
_snapshots: dict[str, dict[str, Any]] = {}

_HEADER_SETS = (
    ("requests", "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests", "x-ratelimit-reset-requests"),
    ("requests", "anthropic-ratelimit-requests-limit", "anthropic-ratelimit-requests-remaining", "anthropic-ratelimit-requests-reset"),
    ("tokens", "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens"),
    ("tokens", "anthropic-ratelimit-tokens-limit", "anthropic-ratelimit-tokens-remaining", "anthropic-ratelimit-tokens-reset"),
    ("tokens", "anthropic-ratelimit-input-tokens-limit", "anthropic-ratelimit-input-tokens-remaining", "anthropic-ratelimit-input-tokens-reset"),
    ("tokens", "anthropic-ratelimit-output-tokens-limit", "anthropic-ratelimit-output-tokens-remaining", "anthropic-ratelimit-output-tokens-reset"),
)


def _reset_seconds(value: str | None) -> float:
    if not value:
        return 60.0
    text = value.strip()
    if text.endswith("Z") or "T" in text:
        try:
            from datetime import datetime, timezone

            return max(1.0, datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() - time.time())
        except ValueError:
            return 60.0
    # OpenAI-style durations, for example "1s", "6m0s", or "1h2m3.5s".
    matches = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", text.lower())
    if not matches:
        try:
            return max(1.0, float(text))
        except ValueError:
            return 60.0
    scales = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    return max(1.0, sum(float(amount) * scales[unit] for amount, unit in matches))


def record_rate_limit(provider_id: str, headers: Any) -> None:
    """Cache a provider's remaining requests/tokens only when both limit values exist."""
    if not provider_id:
        return
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    for unit, limit_header, remaining_header, reset_header in _HEADER_SETS:
        try:
            limit = float(normalized[limit_header])
            remaining = float(normalized[remaining_header])
        except (KeyError, TypeError, ValueError):
            continue
        if limit <= 0 or remaining < 0:
            continue
        now = time.time()
        duration = _reset_seconds(normalized.get(reset_header))
        snapshot = {
            "available": True,
            "kind": "rate_limit",
            "unit": unit,
            "limit": limit,
            "remaining": min(limit, remaining),
            "updated_at": now,
            "expires_at": now + duration + 5,
        }
        with _lock:
            _snapshots[provider_id] = snapshot
        return


def get_rate_limit(provider_id: str) -> dict[str, Any] | None:
    with _lock:
        snapshot = _snapshots.get(provider_id)
        if not snapshot:
            return None
        if snapshot["expires_at"] <= time.time():
            _snapshots.pop(provider_id, None)
            return None
        return dict(snapshot)
