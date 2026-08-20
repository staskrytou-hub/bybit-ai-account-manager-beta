from __future__ import annotations

import copy
import json
from typing import Any

from agents import SQLiteSession

from journal import log_event
from paths import CONVERSATION_DB
from settings import load_settings


def _trim_text(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(1, int(limit * 0.72))
    tail = max(1, limit - head)
    return text[:head] + "\n...[older content trimmed by Stan Context Manager]...\n" + text[-tail:]


def _trim_message_item(item: dict[str, Any], per_message_chars: int) -> dict[str, Any]:
    clean = copy.deepcopy(item)
    content = clean.get("content")
    if isinstance(content, str):
        clean["content"] = _trim_text(content, per_message_chars)
    elif isinstance(content, list):
        remaining = per_message_chars
        for part in content:
            if not isinstance(part, dict):
                continue
            for key in ("text", "content"):
                value = part.get(key)
                if isinstance(value, str):
                    allowed = max(250, min(remaining, per_message_chars))
                    part[key] = _trim_text(value, allowed)
                    remaining = max(0, remaining - len(str(part[key])))
    return clean


def _item_size(item: dict[str, Any]) -> int:
    try:
        return len(json.dumps(item, ensure_ascii=False, default=str))
    except Exception:
        return len(str(item))


class ManagedSQLiteSession:
    """Persistent SQLite session that sends only compact conversational history to the model.

    The underlying SDK session still stores every item (including tool calls), so project/chat
    continuity and local history are preserved. Retrieval intentionally drops old tool-call payloads
    and caps historical message size to prevent large workspaces/projects from re-sending tens of
    thousands of tokens on every turn.
    """

    def __init__(self, session_id: str) -> None:
        cfg = load_settings()
        self.session_id = session_id
        self.session_settings = None
        self._base = SQLiteSession(session_id, CONVERSATION_DB)
        self._message_limit = int(cfg.get("context_history_messages", 10))
        self._char_budget = int(cfg.get("context_history_chars", 18000))
        self._per_message_chars = max(1200, min(6000, self._char_budget // 3))

    async def get_items(self, limit: int | None = None) -> list[Any]:
        requested = self._message_limit if limit is None else max(1, min(int(limit), self._message_limit))
        # Fetch extra items because many stored SDK items may be tool calls/results that we intentionally skip.
        raw = await self._base.get_items(limit=max(40, requested * 8))
        messages: list[dict[str, Any]] = []
        dropped = 0
        for item in raw:
            if not isinstance(item, dict):
                dropped += 1
                continue
            role = str(item.get("role", "")).lower()
            if role not in {"user", "assistant", "system", "developer"}:
                dropped += 1
                continue
            messages.append(_trim_message_item(item, self._per_message_chars))

        messages = messages[-requested:]
        selected_reversed: list[dict[str, Any]] = []
        used = 0
        for item in reversed(messages):
            size = _item_size(item)
            if selected_reversed and used + size > self._char_budget:
                continue
            if not selected_reversed and size > self._char_budget:
                item = _trim_message_item(item, max(1000, self._char_budget - 500))
                size = _item_size(item)
            selected_reversed.append(item)
            used += size
        selected = list(reversed(selected_reversed))
        log_event(
            "context.session_retrieval",
            {
                "session_id": self.session_id,
                "raw_items": len(raw),
                "messages_sent": len(selected),
                "approx_chars": used,
                "dropped_non_messages": dropped,
            },
        )
        return selected

    async def add_items(self, items: list[Any]) -> None:
        await self._base.add_items(items)

    async def pop_item(self) -> Any | None:
        return await self._base.pop_item()

    async def clear_session(self) -> None:
        await self._base.clear_session()


def managed_session(session_id: str) -> ManagedSQLiteSession:
    return ManagedSQLiteSession(session_id)
