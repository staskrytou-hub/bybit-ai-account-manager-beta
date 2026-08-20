from __future__ import annotations

import sqlite3
from typing import Any

from paths import AUTONOMY_DB

RUN_STATUSES = {"planning", "running", "completed", "blocked", "stopped", "budget_exceeded", "failed"}
STEP_STATUSES = {"pending", "in_progress", "passed", "retrying", "blocked", "stopped", "failed"}


def _connect() -> sqlite3.Connection:
    AUTONOMY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(AUTONOMY_DB)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomous_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal TEXT NOT NULL,
            success_criteria TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'planning',
            max_steps INTEGER NOT NULL,
            token_budget INTEGER NOT NULL,
            retry_limit INTEGER NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            final_summary TEXT NOT NULL DEFAULT '',
            stop_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomous_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            step_no INTEGER NOT NULL,
            title TEXT NOT NULL,
            objective TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt INTEGER NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL DEFAULT '',
            lesson TEXT NOT NULL DEFAULT '',
            requests INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, step_no),
            FOREIGN KEY(run_id) REFERENCES autonomous_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.commit()
    return conn


def create_run(goal: str, max_steps: int, token_budget: int, retry_limit: int) -> int:
    goal = goal.strip()
    if not goal:
        raise ValueError("Autonomous goal is required.")
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO autonomous_runs(goal, max_steps, token_budget, retry_limit) VALUES (?, ?, ?, ?)",
            (goal, int(max_steps), int(token_budget), int(retry_limit)),
        )
        return int(cur.lastrowid)


def set_run_plan(run_id: int, success_criteria: str, steps: list[dict[str, str]]) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE autonomous_runs SET success_criteria = ?, status = 'running', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (success_criteria.strip(), int(run_id)),
        )
        conn.execute("DELETE FROM autonomous_steps WHERE run_id = ?", (int(run_id),))
        for index, step in enumerate(steps, start=1):
            conn.execute(
                "INSERT INTO autonomous_steps(run_id, step_no, title, objective) VALUES (?, ?, ?, ?)",
                (int(run_id), index, str(step.get("title", "")).strip(), str(step.get("objective", "")).strip()),
            )


def set_run_status(run_id: int, status: str, *, final_summary: str = "", stop_reason: str = "") -> None:
    status = status.strip().lower()
    if status not in RUN_STATUSES:
        raise ValueError(f"Invalid autonomous run status: {status}")
    with _connect() as conn:
        conn.execute(
            """
            UPDATE autonomous_runs
            SET status = ?, final_summary = CASE WHEN ? <> '' THEN ? ELSE final_summary END,
                stop_reason = CASE WHEN ? <> '' THEN ? ELSE stop_reason END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, final_summary, final_summary, stop_reason, stop_reason, int(run_id)),
        )


def add_run_usage(run_id: int, usage: dict[str, int]) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE autonomous_runs
            SET requests = requests + ?, input_tokens = input_tokens + ?,
                output_tokens = output_tokens + ?, total_tokens = total_tokens + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                int(usage.get("requests", 0)),
                int(usage.get("input_tokens", 0)),
                int(usage.get("output_tokens", 0)),
                int(usage.get("total_tokens", 0)),
                int(run_id),
            ),
        )


def update_step(
    run_id: int,
    step_no: int,
    *,
    status: str,
    attempt: int | None = None,
    summary: str | None = None,
    evidence: str | None = None,
    lesson: str | None = None,
    usage: dict[str, int] | None = None,
) -> None:
    status = status.strip().lower()
    if status not in STEP_STATUSES:
        raise ValueError(f"Invalid autonomous step status: {status}")
    fields = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
    params: list[Any] = [status]
    if attempt is not None:
        fields.append("attempt = ?")
        params.append(int(attempt))
    if summary is not None:
        fields.append("summary = ?")
        params.append(summary)
    if evidence is not None:
        fields.append("evidence = ?")
        params.append(evidence)
    if lesson is not None:
        fields.append("lesson = ?")
        params.append(lesson)
    if usage is not None:
        fields.extend([
            "requests = requests + ?",
            "input_tokens = input_tokens + ?",
            "output_tokens = output_tokens + ?",
            "total_tokens = total_tokens + ?",
        ])
        params.extend([
            int(usage.get("requests", 0)),
            int(usage.get("input_tokens", 0)),
            int(usage.get("output_tokens", 0)),
            int(usage.get("total_tokens", 0)),
        ])
    params.extend([int(run_id), int(step_no)])
    with _connect() as conn:
        conn.execute(
            f"UPDATE autonomous_steps SET {', '.join(fields)} WHERE run_id = ? AND step_no = ?",
            params,
        )


def get_run(run_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, goal, success_criteria, status, max_steps, token_budget, retry_limit,
                   requests, input_tokens, output_tokens, total_tokens, final_summary, stop_reason,
                   created_at, updated_at
            FROM autonomous_runs WHERE id = ?
            """,
            (int(run_id),),
        ).fetchone()
    if not row:
        return None
    keys = [
        "id", "goal", "success_criteria", "status", "max_steps", "token_budget", "retry_limit",
        "requests", "input_tokens", "output_tokens", "total_tokens", "final_summary", "stop_reason",
        "created_at", "updated_at",
    ]
    return dict(zip(keys, row))


def list_runs(limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, goal, status, total_tokens, max_steps, token_budget, created_at, updated_at
            FROM autonomous_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    return [
        {
            "id": row[0], "goal": row[1], "status": row[2], "total_tokens": row[3],
            "max_steps": row[4], "token_budget": row[5], "created_at": row[6], "updated_at": row[7],
        }
        for row in rows
    ]


def list_steps(run_id: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT step_no, title, objective, status, attempt, summary, evidence, lesson,
                   requests, input_tokens, output_tokens, total_tokens, created_at, updated_at
            FROM autonomous_steps
            WHERE run_id = ?
            ORDER BY step_no ASC
            """,
            (int(run_id),),
        ).fetchall()
    keys = [
        "step_no", "title", "objective", "status", "attempt", "summary", "evidence", "lesson",
        "requests", "input_tokens", "output_tokens", "total_tokens", "created_at", "updated_at",
    ]
    return [dict(zip(keys, row)) for row in rows]


def autonomous_usage_today() -> int:
    """Total autonomous-run tokens used today (local time approximation via SQLite)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) FROM autonomous_runs WHERE date(created_at, 'localtime') = date('now', 'localtime')"
        ).fetchone()
    return int(row[0])
