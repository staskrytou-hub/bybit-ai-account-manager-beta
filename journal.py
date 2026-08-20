from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from paths import ACTION_LOG


def log_event(event: str, payload: dict[str, Any]) -> None:
    ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "payload": payload,
    }
    with ACTION_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def recent_events(limit: int = 200) -> list[dict[str, Any]]:
    if not ACTION_LOG.exists():
        return []
    lines = ACTION_LOG.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 1000)):]
    items: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            items.append(value)
    return items
