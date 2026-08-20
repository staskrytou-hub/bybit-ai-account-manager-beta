from __future__ import annotations

import sqlite3

from paths import USAGE_DB


def _connect() -> sqlite3.Connection:
    USAGE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(USAGE_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            requests INTEGER NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL
        )
        """
    )
    return conn


def record_usage(usage: dict[str, int]) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO usage_runs(requests, input_tokens, output_tokens, total_tokens)
            VALUES (?, ?, ?, ?)
            """,
            (
                int(usage.get("requests", 0)),
                int(usage.get("input_tokens", 0)),
                int(usage.get("output_tokens", 0)),
                int(usage.get("total_tokens", 0)),
            ),
        )


def usage_totals() -> dict[str, int]:
    return _totals("")


def usage_today() -> dict[str, int]:
    return _totals("WHERE date(created_at, 'localtime') = date('now', 'localtime')")


def _totals(where: str) -> dict[str, int]:
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(requests), 0),
                   COALESCE(SUM(input_tokens), 0),
                   COALESCE(SUM(output_tokens), 0),
                   COALESCE(SUM(total_tokens), 0)
            FROM usage_runs
            {where}
            """
        ).fetchone()
    return {
        "requests": int(row[0]),
        "input_tokens": int(row[1]),
        "output_tokens": int(row[2]),
        "total_tokens": int(row[3]),
    }
