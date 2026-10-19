"""Simple JSON-file task history. No database needed for a demo-scale agent."""
import json
import threading
import time
import uuid
from typing import Any, Optional

from . import config

_lock = threading.Lock()


def _read_all() -> list[dict[str, Any]]:
    if not config.HISTORY_FILE.exists():
        return []
    try:
        return json.loads(config.HISTORY_FILE.read_text())
    except Exception:
        return []


def _write_all(items: list[dict[str, Any]]) -> None:
    config.HISTORY_FILE.write_text(json.dumps(items, indent=2))


def create_entry(task: str) -> dict[str, Any]:
    entry = {
        "id": uuid.uuid4().hex[:10],
        "task": task,
        "started_at": time.time(),
        "finished_at": None,
        "duration_s": None,
        "status": "running",  # running | success | failed | cancelled
        "steps": [],
        "log": [],
        "result": None,
        "trace": [],           # structured per-action execution trace (see main.py)
        "plan_source": None,   # "llm" | "deterministic"
        "plan_reason": None,   # why, if the LLM plan wasn't used
        "mode": None,           # "OpenRouter Free" | "Local Demo Fallback" — same info, human-readable
    }
    with _lock:
        items = _read_all()
        items.insert(0, entry)
        items = items[:200]  # cap history size
        _write_all(items)
    return entry


def update_entry(entry_id: str, **fields) -> None:
    with _lock:
        items = _read_all()
        for item in items:
            if item["id"] == entry_id:
                item.update(fields)
                break
        _write_all(items)


def finish_entry(entry_id: str, status: str, result: Optional[dict] = None) -> None:
    with _lock:
        items = _read_all()
        for item in items:
            if item["id"] == entry_id:
                item["status"] = status
                item["finished_at"] = time.time()
                item["duration_s"] = round(item["finished_at"] - item["started_at"], 1)
                if result is not None:
                    item["result"] = result
                break
        _write_all(items)


def list_entries() -> list[dict[str, Any]]:
    return _read_all()


def get_entry(entry_id: str) -> Optional[dict[str, Any]]:
    for item in _read_all():
        if item["id"] == entry_id:
            return item
    return None
