from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from paths import CHAT_STORE_DB, CONVERSATION_DB
from text_safety import sanitize_text


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CHAT_STORE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    CHAT_STORE_DB.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                session_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_chats_project_updated
                ON chats(project_id, updated_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_chat_id
                ON messages(chat_id, id ASC);
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )


def _extract_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        direct = item.get("text")
        if isinstance(direct, str):
            return direct
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [_extract_text(part) for part in content]
            return "\n".join(part for part in parts if part).strip()
    if isinstance(item, list):
        parts = [_extract_text(part) for part in item]
        return "\n".join(part for part in parts if part).strip()
    return ""


def _import_legacy_main(chat_id: int) -> int:
    if not CONVERSATION_DB.exists():
        return 0
    try:
        conn = sqlite3.connect(CONVERSATION_DB)
        rows = conn.execute(
            "SELECT message_data FROM agent_messages WHERE session_id=? ORDER BY id ASC",
            ("main",),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return 0

    imported = 0
    now = _now()
    with _connect() as target:
        existing = target.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,)).fetchone()[0]
        if existing:
            return 0
        for (raw,) in rows:
            try:
                item = json.loads(raw)
            except Exception:
                continue
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip().lower()
            if role not in {"user", "assistant"}:
                continue
            text = _extract_text(item).strip()
            if not text:
                continue
            target.execute(
                "INSERT INTO messages(chat_id, role, content, meta_json, created_at) VALUES(?,?,?,?,?)",
                (chat_id, role, text, json.dumps({"migrated": True}), now),
            )
            imported += 1
    return imported


def ensure_default_structure() -> tuple[dict[str, Any], dict[str, Any]]:
    _init_db()
    with _connect() as conn:
        project = conn.execute("SELECT * FROM projects ORDER BY id ASC LIMIT 1").fetchone()
        if project is None:
            now = _now()
            cur = conn.execute(
                "INSERT INTO projects(name, created_at, updated_at) VALUES(?,?,?)",
                ("General", now, now),
            )
            project_id = int(cur.lastrowid)
        else:
            project_id = int(project["id"])

        chat = conn.execute("SELECT * FROM chats ORDER BY id ASC LIMIT 1").fetchone()
        created_chat = False
        if chat is None:
            now = _now()
            cur = conn.execute(
                "INSERT INTO chats(project_id, title, session_id, created_at, updated_at) VALUES(?,?,?,?,?)",
                (project_id, "Previous chat", "main", now, now),
            )
            chat_id = int(cur.lastrowid)
            created_chat = True
        else:
            chat_id = int(chat["id"])

        active_project = conn.execute("SELECT value FROM app_state WHERE key='active_project_id'").fetchone()
        active_chat = conn.execute("SELECT value FROM app_state WHERE key='active_chat_id'").fetchone()
        try:
            active_project_id = int(active_project[0]) if active_project else project_id
        except Exception:
            active_project_id = project_id
        try:
            active_chat_id = int(active_chat[0]) if active_chat else chat_id
        except Exception:
            active_chat_id = chat_id

        valid_chat = conn.execute("SELECT project_id FROM chats WHERE id=?", (active_chat_id,)).fetchone()
        if valid_chat is None:
            active_chat_id = chat_id
            active_project_id = project_id
        else:
            active_project_id = int(valid_chat["project_id"])

        conn.execute(
            "INSERT INTO app_state(key,value) VALUES('active_project_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(active_project_id),),
        )
        conn.execute(
            "INSERT INTO app_state(key,value) VALUES('active_chat_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(active_chat_id),),
        )

    if created_chat:
        imported = _import_legacy_main(chat_id)
        if imported == 0:
            rename_chat(chat_id, "Main chat")
        else:
            rename_chat(chat_id, "Previous conversation")

    project = get_project(active_project_id) or get_project(project_id)
    chat = get_chat(active_chat_id) or get_chat(chat_id)
    assert project is not None and chat is not None
    return project, chat


def get_project(project_id: int) -> dict[str, Any] | None:
    _init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return dict(row) if row else None


def list_projects() -> list[dict[str, Any]]:
    _init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC, id DESC").fetchall()
    return [dict(row) for row in rows]


def create_project(name: str) -> int:
    name = (name or "").strip() or "New project"
    now = _now()
    with _connect() as conn:
        cur = conn.execute("INSERT INTO projects(name,created_at,updated_at) VALUES(?,?,?)", (name[:100], now, now))
        project_id = int(cur.lastrowid)
    create_chat(project_id, "New chat")
    return project_id


def rename_project(project_id: int, name: str) -> bool:
    name = (name or "").strip()
    if not name:
        return False
    with _connect() as conn:
        cur = conn.execute("UPDATE projects SET name=?, updated_at=? WHERE id=?", (name[:100], _now(), project_id))
    return cur.rowcount > 0


def delete_project(project_id: int) -> bool:
    with _connect() as conn:
        count = int(conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0])
        if count <= 1:
            return False
        cur = conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    return cur.rowcount > 0


def list_chats(project_id: int) -> list[dict[str, Any]]:
    _init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chats WHERE project_id=? ORDER BY updated_at DESC, id DESC",
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_chat(chat_id: int) -> dict[str, Any] | None:
    _init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    return dict(row) if row else None


def create_chat(project_id: int, title: str = "New chat", session_id: str | None = None) -> int:
    now = _now()
    sid = session_id or f"chat-{uuid.uuid4().hex}"
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO chats(project_id,title,session_id,created_at,updated_at) VALUES(?,?,?,?,?)",
            (project_id, (title or "New chat")[:120], sid, now, now),
        )
        conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, project_id))
    return int(cur.lastrowid)


def rename_chat(chat_id: int, title: str) -> bool:
    title = (title or "").strip()
    if not title:
        return False
    with _connect() as conn:
        cur = conn.execute("UPDATE chats SET title=?, updated_at=? WHERE id=?", (title[:120], _now(), chat_id))
    return cur.rowcount > 0


def maybe_title_chat(chat_id: int, first_user_text: str) -> str:
    chat = get_chat(chat_id)
    if chat is None:
        return ""
    current = str(chat.get("title", ""))
    if current not in {"New chat", "Main chat"}:
        return current
    clean = " ".join((first_user_text or "").strip().split())
    if not clean:
        return current
    title = clean[:56] + ("..." if len(clean) > 56 else "")
    rename_chat(chat_id, title)
    return title


def delete_chat(chat_id: int) -> bool:
    chat = get_chat(chat_id)
    if chat is None:
        return False
    project_id = int(chat["project_id"])
    with _connect() as conn:
        count = int(conn.execute("SELECT COUNT(*) FROM chats WHERE project_id=?", (project_id,)).fetchone()[0])
        if count <= 1:
            return False
        cur = conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
    return cur.rowcount > 0


def add_message(chat_id: int, role: str, content: str, meta: dict[str, Any] | None = None) -> int:
    text = sanitize_text(str(content or ""), context=False).strip()
    if not text:
        return 0
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO messages(chat_id,role,content,meta_json,created_at) VALUES(?,?,?,?,?)",
            (chat_id, role, text, json.dumps(meta or {}, ensure_ascii=False), now),
        )
        row = conn.execute("SELECT project_id FROM chats WHERE id=?", (chat_id,)).fetchone()
        conn.execute("UPDATE chats SET updated_at=? WHERE id=?", (now, chat_id))
        if row:
            conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, int(row[0])))
    return int(cur.lastrowid)


def list_messages(chat_id: int, limit: int = 2000) -> list[dict[str, Any]]:
    _init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, max(1, int(limit))),
        ).fetchall()
    result = [dict(row) for row in reversed(rows)]
    for item in result:
        try:
            item["meta"] = json.loads(item.pop("meta_json", "{}"))
        except Exception:
            item["meta"] = {}
    return result


def set_active(project_id: int, chat_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO app_state(key,value) VALUES('active_project_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(project_id),),
        )
        conn.execute(
            "INSERT INTO app_state(key,value) VALUES('active_chat_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(chat_id),),
        )


def build_compact_context(chat_id: int, recent_limit: int = 8, max_chars: int = 14000) -> str:
    """Build compact continuity context without deleting the full locally stored chat history."""
    _init_db()
    recent_limit = max(2, min(int(recent_limit), 30))
    max_chars = max(3000, min(int(max_chars), 50000))
    with _connect() as conn:
        first_user = conn.execute(
            "SELECT id, content FROM messages WHERE chat_id=? AND role='user' ORDER BY id ASC LIMIT 1",
            (chat_id,),
        ).fetchone()
        rows = conn.execute(
            "SELECT id, role, content FROM messages WHERE chat_id=? AND role IN ('user','assistant') ORDER BY id DESC LIMIT ?",
            (chat_id, recent_limit),
        ).fetchall()

    recent = [dict(row) for row in reversed(rows)]
    parts: list[str] = []
    if first_user is not None:
        original = sanitize_text(str(first_user["content"] or ""), context=True).strip()
        if original:
            original = original[:5000]
            parts.append("ORIGINAL USER REQUEST / PROJECT START:\n" + original)

    recent_parts: list[str] = []
    first_id = int(first_user["id"]) if first_user is not None else -1
    for row in recent:
        if int(row["id"]) == first_id:
            continue
        role = "USER" if str(row["role"]) == "user" else "STAN"
        text = sanitize_text(str(row["content"] or ""), context=True).strip()
        if not text:
            continue
        # Historical assistant messages can contain large generated code. Keep intent/results, not giant payloads.
        per_item = 2800 if role == "USER" else 2200
        if len(text) > per_item:
            text = text[:per_item] + "\n...[historical message trimmed]..."
        recent_parts.append(f"{role}: {text}")
    if recent_parts:
        parts.append("RECENT CONTINUITY:\n" + "\n\n".join(recent_parts))

    text = "\n\n".join(parts)
    if len(text) <= max_chars:
        return text
    # Preserve the original request and the newest context when trimming.
    head = min(5200, max_chars // 2)
    tail = max_chars - head - 80
    return text[:head] + "\n\n...[older continuity trimmed]...\n\n" + text[-tail:]
