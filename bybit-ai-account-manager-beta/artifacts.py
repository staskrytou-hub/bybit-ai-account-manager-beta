from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from paths import ARTIFACTS_DB, WORKSPACE_DIR


def _connect() -> sqlite3.Connection:
    ARTIFACTS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ARTIFACTS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            relative_path TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL DEFAULT 'file',
            source TEXT NOT NULL DEFAULT 'agent',
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(artifacts)").fetchall()}
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE artifacts ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS artifact_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            relative_path TEXT NOT NULL,
            kind TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def register_artifact(relative_path: str, *, kind: str = "file", source: str = "agent", description: str = "") -> int:
    rel = str(relative_path).replace("\\", "/").lstrip("/")
    path = (WORKSPACE_DIR / rel).resolve()
    root = WORKSPACE_DIR.resolve()
    if root not in path.parents and path != root:
        raise ValueError("Artifact must stay inside Bybit AI Manager Workspace.")
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(rel)
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO artifacts(relative_path, kind, source, description, created_at, updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(relative_path) DO UPDATE SET
                kind=excluded.kind,
                source=excluded.source,
                description=excluded.description,
                updated_at=excluded.updated_at
            """,
            (rel, kind[:40], source[:40], description[:500], now, now),
        )
        cur = conn.execute(
            "INSERT INTO artifact_events(relative_path, kind, source, created_at) VALUES(?,?,?,?)",
            (rel, kind[:40], source[:40], now),
        )
        event_id = int(cur.lastrowid)
    return event_id


def artifact_watermark() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM artifact_events").fetchone()
    return int(row[0])


def _decorate(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        path = (WORKSPACE_DIR / str(item["relative_path"])).resolve()
        item["exists"] = path.exists() and path.is_file()
        item["size_bytes"] = path.stat().st_size if item["exists"] else 0
        item["absolute_path"] = str(path)
        result.append(item)
    return result


def list_artifacts(*, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 1000))
    with _connect() as conn:
        if int(after_id) > 0:
            # Return the current artifact state for every path touched after the watermark.
            rows = conn.execute(
                """
                SELECT a.* FROM artifacts a
                JOIN (
                    SELECT relative_path, MAX(id) AS event_id
                    FROM artifact_events
                    WHERE id > ?
                    GROUP BY relative_path
                ) e ON e.relative_path = a.relative_path
                ORDER BY e.event_id DESC
                LIMIT ?
                """,
                (int(after_id), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM artifacts ORDER BY CASE WHEN updated_at='' THEN created_at ELSE updated_at END DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return _decorate(rows)


def sync_workspace_artifacts() -> int:
    count = 0
    with _connect() as conn:
        known = {str(r[0]) for r in conn.execute("SELECT relative_path FROM artifacts").fetchall()}
    for path in WORKSPACE_DIR.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(WORKSPACE_DIR).as_posix()
        if rel in known:
            continue
        suffix = path.suffix.lower()
        kind = "image" if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"} else "file"
        register_artifact(rel, kind=kind, source="workspace_sync")
        count += 1
    return count
