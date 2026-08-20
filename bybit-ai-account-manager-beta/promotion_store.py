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
        CREATE TABLE IF NOT EXISTS promotion_scans(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          scanned_at TEXT NOT NULL,
          region_hint TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL DEFAULT '',
          usage_json TEXT NOT NULL DEFAULT '{}',
          summary_json TEXT NOT NULL DEFAULT '{}',
          error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS promotions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          scan_id INTEGER NOT NULL,
          scanned_at TEXT NOT NULL,
          campaign_key TEXT NOT NULL,
          name TEXT NOT NULL,
          source_url TEXT NOT NULL DEFAULT '',
          source_title TEXT NOT NULL DEFAULT '',
          region TEXT NOT NULL DEFAULT '',
          starts_at TEXT NOT NULL DEFAULT '',
          ends_at TEXT NOT NULL DEFAULT '',
          reward_type TEXT NOT NULL DEFAULT '',
          reward_value_estimate_usd REAL,
          probabilistic INTEGER NOT NULL DEFAULT 0,
          requires_registration INTEGER NOT NULL DEFAULT 0,
          account_specific INTEGER NOT NULL DEFAULT 0,
          trading_volume_requirement_usd REAL,
          eligible_symbols_json TEXT NOT NULL DEFAULT '[]',
          tasks_json TEXT NOT NULL DEFAULT '[]',
          restrictions_json TEXT NOT NULL DEFAULT '[]',
          safety_flags_json TEXT NOT NULL DEFAULT '[]',
          actionability TEXT NOT NULL DEFAULT 'track',
          confidence REAL NOT NULL DEFAULT 0,
          raw_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_promotions_scan ON promotions(scan_id, id);
        CREATE INDEX IF NOT EXISTS idx_promotions_key ON promotions(campaign_key, scanned_at);
        """
    )
    conn.commit()
    return conn


def _official_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return u.startswith("https://www.bybit.com/") or u.startswith("https://bybit.com/") or u.startswith("https://www.bybit.eu/") or u.startswith("https://bybit.eu/") or u.startswith("https://announcements.bybit.com/") or u.startswith("https://announcements.bybit.eu/")


def store_promotion_scan(
    *,
    region_hint: str,
    model: str,
    usage: dict[str, Any],
    summary: dict[str, Any],
) -> int:
    campaigns = summary.get("campaigns") if isinstance(summary, dict) else []
    if not isinstance(campaigns, list):
        campaigns = []
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO promotion_scans(scanned_at,region_hint,model,usage_json,summary_json) VALUES(?,?,?,?,?)",
            (_now(), region_hint[:120], model[:120], json.dumps(usage, ensure_ascii=False), json.dumps(summary, ensure_ascii=False)),
        )
        scan_id = int(cur.lastrowid)
        for index, campaign in enumerate(campaigns):
            if not isinstance(campaign, dict):
                continue
            source_url = str(campaign.get("source_url", "")).strip()
            if source_url and not _official_url(source_url):
                # Promotion Intelligence is deliberately restricted to official Bybit properties.
                source_url = ""
            name = str(campaign.get("name", "Unnamed campaign")).strip()[:300]
            key = str(campaign.get("campaign_key", "")).strip() or f"scan{scan_id}-{index}-{name.lower().replace(' ', '-')[:80]}"
            symbols = campaign.get("eligible_symbols") if isinstance(campaign.get("eligible_symbols"), list) else []
            tasks = campaign.get("tasks") if isinstance(campaign.get("tasks"), list) else []
            restrictions = campaign.get("restrictions") if isinstance(campaign.get("restrictions"), list) else []
            safety = campaign.get("safety_flags") if isinstance(campaign.get("safety_flags"), list) else []
            conn.execute(
                """
                INSERT INTO promotions(
                  scan_id,scanned_at,campaign_key,name,source_url,source_title,region,starts_at,ends_at,reward_type,
                  reward_value_estimate_usd,probabilistic,requires_registration,account_specific,trading_volume_requirement_usd,
                  eligible_symbols_json,tasks_json,restrictions_json,safety_flags_json,actionability,confidence,raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    scan_id, _now(), key[:180], name, source_url, str(campaign.get("source_title", ""))[:300],
                    str(campaign.get("region", ""))[:180], str(campaign.get("starts_at", ""))[:80], str(campaign.get("ends_at", ""))[:80],
                    str(campaign.get("reward_type", ""))[:100],
                    float(campaign["reward_value_estimate_usd"]) if campaign.get("reward_value_estimate_usd") is not None else None,
                    int(bool(campaign.get("probabilistic"))), int(bool(campaign.get("requires_registration"))), int(bool(campaign.get("account_specific"))),
                    float(campaign["trading_volume_requirement_usd"]) if campaign.get("trading_volume_requirement_usd") is not None else None,
                    json.dumps(symbols, ensure_ascii=False), json.dumps(tasks, ensure_ascii=False), json.dumps(restrictions, ensure_ascii=False),
                    json.dumps(safety, ensure_ascii=False), str(campaign.get("actionability", "track"))[:80],
                    max(0.0, min(1.0, float(campaign.get("confidence", 0.0) or 0.0))), json.dumps(campaign, ensure_ascii=False),
                ),
            )
    return scan_id


def latest_promotion_scan() -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM promotion_scans ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        item = dict(row)
        promos = conn.execute("SELECT * FROM promotions WHERE scan_id=? ORDER BY confidence DESC,id ASC", (int(item["id"]),)).fetchall()
    for key in ("usage_json", "summary_json"):
        try:
            item[key[:-5]] = json.loads(item.pop(key))
        except Exception:
            item[key[:-5]] = {}
    campaigns: list[dict[str, Any]] = []
    for row in promos:
        p = dict(row)
        for key in ("eligible_symbols_json", "tasks_json", "restrictions_json", "safety_flags_json", "raw_json"):
            try:
                p[key[:-5]] = json.loads(p.pop(key))
            except Exception:
                p[key[:-5]] = [] if key != "raw_json" else {}
        campaigns.append(p)
    item["campaigns"] = campaigns
    return item


def promotion_context_for_symbol(symbol: str) -> list[dict[str, Any]]:
    latest = latest_promotion_scan()
    if not latest:
        return []
    target = symbol.upper()
    out: list[dict[str, Any]] = []
    for c in latest.get("campaigns", []):
        symbols = [str(x).upper() for x in (c.get("eligible_symbols") or [])]
        if target not in symbols:
            continue
        out.append({
            "type": "promotion_alignment",
            "name": c.get("name"),
            "ends_at": c.get("ends_at"),
            "reward_type": c.get("reward_type"),
            "trading_volume_requirement_usd": c.get("trading_volume_requirement_usd"),
            "actionability": c.get("actionability"),
            "safety_flags": c.get("safety_flags", []),
            "rule": "Promotion alignment may never override strategy quality, risk limits, fees/slippage, or create artificial volume.",
        })
    return out[:4]


def promotion_symbol_boosts() -> dict[str, float]:
    """Return a deliberately tiny score hint for symbols in safe, current-looking campaigns.

    The trading scanner applies this only AFTER a market has a reasonable base setup score.
    It can never turn a weak market into a trade candidate or bypass the risk engine.
    """
    latest = latest_promotion_scan()
    if not latest:
        return {}
    boosts: dict[str, float] = {}
    for c in latest.get("campaigns", []):
        flags = {str(x).lower() for x in (c.get("safety_flags") or [])}
        if {"wash_trading", "matched_trading", "volume_faking", "unverified_source"} & flags:
            continue
        actionability = str(c.get("actionability", "track"))
        if actionability not in {"trade_alignment", "track", "manual_registration"}:
            continue
        conf = max(0.0, min(1.0, float(c.get("confidence", 0.0) or 0.0)))
        for raw in c.get("eligible_symbols") or []:
            symbol = str(raw).upper().strip()
            if not symbol.endswith("USDT"):
                continue
            boosts[symbol] = max(boosts.get(symbol, 0.0), conf)
    return boosts
