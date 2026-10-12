"""Local append-only model performance log."""

import json
import threading
from pathlib import Path
from typing import Any


_LOG_PATH = Path(__file__).with_name("model-performance.jsonl")
_LOG_LOCK = threading.Lock()


def append_model_performance(entry: dict[str, Any]) -> None:
    """Append a single metrics-only record without storing chat content."""
    serialized = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    with _LOG_LOCK, _LOG_PATH.open("a", encoding="utf-8", newline="\n") as log_file:
        log_file.write(serialized + "\n")
        log_file.flush()


def read_model_performance(model_id: str | None = None, model_name: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Read recent measurements for one model from this computer's local log."""
    if not _LOG_PATH.exists():
        return []
    id_filter = (model_id or "").strip().casefold()
    name_filter = (model_name or "").strip().casefold()
    with _LOG_LOCK, _LOG_PATH.open("r", encoding="utf-8") as log_file:
        entries = []
        for line in log_file:
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            same_id = id_filter and str(entry.get("model_id", "")).strip().casefold() == id_filter
            same_name = name_filter and str(entry.get("model_name", "")).strip().casefold() == name_filter
            if (not id_filter and not name_filter) or same_id or same_name:
                entries.append(entry)
    return entries[-max(1, min(int(limit), 500)):][::-1]
