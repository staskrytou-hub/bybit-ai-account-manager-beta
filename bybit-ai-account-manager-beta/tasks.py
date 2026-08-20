from __future__ import annotations

import sqlite3

from paths import TASKS_DB

VALID_STATUSES = {"todo", "in_progress", "blocked", "done", "cancelled"}
VALID_PRIORITIES = {"low", "normal", "high"}


def _connect() -> sqlite3.Connection:
    TASKS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(TASKS_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'todo',
            priority TEXT NOT NULL DEFAULT 'normal',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return conn


def create_task(title: str, details: str = "", priority: str = "normal") -> int:
    title = title.strip()
    details = details.strip()
    priority = priority.strip().lower()
    if not title:
        raise ValueError("Task title is required.")
    if priority not in VALID_PRIORITIES:
        priority = "normal"
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO tasks(title, details, priority) VALUES (?, ?, ?)",
            (title, details, priority),
        )
        return int(cur.lastrowid)


def update_task(task_id: int, status: str, details: str = "") -> bool:
    status = status.strip().lower()
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid task status: {status}")
    with _connect() as conn:
        if details.strip():
            cur = conn.execute(
                "UPDATE tasks SET status = ?, details = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, details.strip(), int(task_id)),
            )
        else:
            cur = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, int(task_id)),
            )
        return cur.rowcount > 0


def list_tasks(status: str = "", limit: int = 200) -> list[dict[str, object]]:
    status = status.strip().lower()
    params: list[object] = []
    where = ""
    if status in VALID_STATUSES:
        where = "WHERE status = ?"
        params.append(status)
    params.append(max(1, min(limit, 500)))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, title, details, status, priority, created_at, updated_at
            FROM tasks
            {where}
            ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                     CASE status WHEN 'in_progress' THEN 0 WHEN 'todo' THEN 1 WHEN 'blocked' THEN 2 ELSE 3 END,
                     id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [
        {
            "id": r[0], "title": r[1], "details": r[2], "status": r[3],
            "priority": r[4], "created_at": r[5], "updated_at": r[6]
        }
        for r in rows
    ]


def delete_task(task_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (int(task_id),))
        return cur.rowcount > 0
