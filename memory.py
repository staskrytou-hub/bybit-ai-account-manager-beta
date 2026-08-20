from __future__ import annotations

import sqlite3

from paths import MEMORY_DB

VALID_CATEGORIES = {"profile", "preference", "rule", "project", "lesson", "reference", "other"}


def _connect() -> sqlite3.Connection:
    MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(MEMORY_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "category" not in columns:
        conn.execute("ALTER TABLE memories ADD COLUMN category TEXT NOT NULL DEFAULT 'other'")
    if "importance" not in columns:
        conn.execute("ALTER TABLE memories ADD COLUMN importance INTEGER NOT NULL DEFAULT 3")
    conn.commit()
    return conn


def _clean_category(category: str) -> str:
    value = category.strip().lower() or "other"
    return value if value in VALID_CATEGORIES else "other"


def save_memory(topic: str, content: str, category: str = "other", importance: int = 3) -> int:
    topic = topic.strip()
    content = content.strip()
    category = _clean_category(category)
    importance = max(1, min(int(importance), 5))
    if not topic or not content:
        raise ValueError("Memory topic and content are required.")
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO memories(topic, content, category, importance) VALUES (?, ?, ?, ?)",
            (topic, content, category, importance),
        )
        return int(cur.lastrowid)


def search_memories(query: str, category: str = "", limit: int = 8) -> list[dict[str, object]]:
    query = query.strip()
    category = category.strip().lower()
    clauses: list[str] = []
    params: list[object] = []
    if query:
        pattern = f"%{query}%"
        clauses.append("(topic LIKE ? OR content LIKE ?)")
        params.extend([pattern, pattern])
    if category in VALID_CATEGORIES:
        clauses.append("category = ?")
        params.append(category)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(limit, 50)))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, topic, content, category, importance, created_at
            FROM memories
            {where}
            ORDER BY importance DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [
        {
            "id": r[0], "topic": r[1], "content": r[2], "category": r[3],
            "importance": r[4], "created_at": r[5]
        }
        for r in rows
    ]


def list_memories(limit: int = 100, category: str = "") -> list[dict[str, object]]:
    return search_memories("", category=category, limit=limit)


def delete_memory(memory_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
        return cur.rowcount > 0
