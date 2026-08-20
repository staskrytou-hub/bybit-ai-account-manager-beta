from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from paths import TRADING_RESEARCH_DB


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    TRADING_RESEARCH_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(TRADING_RESEARCH_DB, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS bootstrap_runs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at TEXT NOT NULL,
          finished_at TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'running',
          key_environment TEXT NOT NULL DEFAULT '',
          report_json TEXT NOT NULL DEFAULT '{}',
          summary TEXT NOT NULL DEFAULT '',
          error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS strategy_results(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          bootstrap_id INTEGER NOT NULL,
          symbol TEXT NOT NULL,
          interval TEXT NOT NULL,
          strategy TEXT NOT NULL,
          result_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS research_state(
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )
    conn.commit()
    return conn


def create_bootstrap(key_environment: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO bootstrap_runs(started_at,status,key_environment) VALUES(?,?,?)",
            (_now(), "running", key_environment),
        )
        return int(cur.lastrowid)


def finish_bootstrap(run_id: int, *, status: str, report: dict[str, Any], summary: str = "", error: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE bootstrap_runs SET finished_at=?, status=?, report_json=?, summary=?, error=? WHERE id=?",
            (_now(), status, json.dumps(report, ensure_ascii=False), summary, error, int(run_id)),
        )
        if status == "completed":
            conn.execute(
                "INSERT INTO research_state(key,value) VALUES('bootstrap_complete','1') ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            conn.execute(
                "INSERT INTO research_state(key,value) VALUES('last_bootstrap_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_now(),),
            )


def store_strategy_result(run_id: int, symbol: str, interval: str, result: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO strategy_results(ts,bootstrap_id,symbol,interval,strategy,result_json) VALUES(?,?,?,?,?,?)",
            (_now(), int(run_id), symbol.upper(), str(interval), str(result.get("strategy", "unknown")), json.dumps(result, ensure_ascii=False)),
        )


def get_research_state(key: str, default: str = "") -> str:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM research_state WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def set_research_state(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO research_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def latest_bootstrap() -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM bootstrap_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["report"] = json.loads(item.pop("report_json"))
    except Exception:
        item["report"] = {}
    return item


def strategy_leaderboard(limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM strategy_results ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 1000)),)).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            result = json.loads(item.pop("result_json"))
        except Exception:
            result = {}
        item.update(result)
        out.append(item)
    out.sort(key=lambda x: (float(x.get("expectancy_r", -999)), float(x.get("profit_factor", 0)), int(x.get("trades", 0))), reverse=True)
    return out


def research_context_for_symbol(symbol: str, interval: str, limit: int = 4) -> list[dict[str, Any]]:
    symbol = symbol.upper()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_results WHERE symbol=? AND interval=? ORDER BY id DESC LIMIT 80",
            (symbol, str(interval)),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            result = json.loads(item.get("result_json", "{}"))
        except Exception:
            result = {}
        out.append({
            "strategy": result.get("strategy", item.get("strategy")),
            "name": result.get("name"),
            "robust": result.get("robust"),
            "robustness_score": result.get("robustness_score"),
            "full": result.get("full", {}),
            "out_of_sample": result.get("out_of_sample", {}),
        })
    out.sort(key=lambda x: float(x.get("robustness_score", 0) or 0), reverse=True)
    selected = out[: max(1, min(int(limit), 10))]
    baseline = latest_bootstrap()
    if baseline:
        report = baseline.get("report") or {}
        chief = report.get("chief_research") or {}
        if chief:
            selected.append({
                "type": "professional_baseline",
                "market_regime": chief.get("market_regime", ""),
                "priority_symbols": chief.get("priority_symbols", []),
                "major_risks": chief.get("major_risks", []),
                "operating_rules": chief.get("operating_rules", []),
            })
    return selected
