from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from paths import DATA_DIR

DB = DATA_DIR / "account_os.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS core_state(
          key TEXT PRIMARY KEY,
          value_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS core_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          kind TEXT NOT NULL,
          message TEXT NOT NULL,
          payload_json TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    conn.commit()
    return conn


def set_state(key: str, value: Any) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO core_state(key,value_json,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False, default=str), _now()),
        )


def get_state(key: str, default: Any = None) -> Any:
    with _connect() as conn:
        row = conn.execute("SELECT value_json FROM core_state WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except Exception:
        return default


def record_event(kind: str, message: str, payload: dict[str, Any] | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO core_events(ts,kind,message,payload_json) VALUES(?,?,?,?)",
            (_now(), kind[:80], message[:1200], json.dumps(payload or {}, ensure_ascii=False, default=str)),
        )
        conn.execute(
            "DELETE FROM core_events WHERE id NOT IN (SELECT id FROM core_events ORDER BY id DESC LIMIT 2000)"
        )


def recent_events(limit: int = 80) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM core_events ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),)).fetchall()
    out=[]
    for row in rows:
        item=dict(row)
        try: item["payload"] = json.loads(item.pop("payload_json"))
        except Exception: item["payload"] = {}
        out.append(item)
    return out
