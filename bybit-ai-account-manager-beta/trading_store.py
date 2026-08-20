from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from paths import TRADING_DB


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    TRADING_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(TRADING_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS cycles(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, symbol TEXT NOT NULL, interval TEXT NOT NULL,
      mode TEXT NOT NULL, snapshot_json TEXT NOT NULL, local_signal TEXT NOT NULL DEFAULT '', ai_called INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS assessments(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, symbol TEXT NOT NULL, candle_start_ms INTEGER NOT NULL,
      action TEXT NOT NULL, confidence REAL NOT NULL, assessment_json TEXT NOT NULL, risk_json TEXT NOT NULL DEFAULT '{}', execution_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS paper_positions(
      symbol TEXT PRIMARY KEY, side TEXT NOT NULL, qty REAL NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL, take_profit REAL NOT NULL,
      opened_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', unrealized REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS paper_trades(
      id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, entry REAL NOT NULL,
      exit REAL NOT NULL, pnl REAL NOT NULL, reason TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    conn.commit()
    return conn


def record_cycle(symbol: str, interval: str, mode: str, snapshot: dict[str, Any], local_signal: str, ai_called: bool) -> int:
    with _connect() as conn:
        cur=conn.execute('INSERT INTO cycles(ts,symbol,interval,mode,snapshot_json,local_signal,ai_called) VALUES(?,?,?,?,?,?,?)',(_now(),symbol,interval,mode,json.dumps(snapshot,ensure_ascii=False),local_signal,int(ai_called)))
        return int(cur.lastrowid)


def record_assessment(symbol: str, candle_start_ms: int, assessment: dict[str, Any], risk: dict[str, Any], execution: dict[str, Any]) -> int:
    with _connect() as conn:
        cur=conn.execute('INSERT INTO assessments(ts,symbol,candle_start_ms,action,confidence,assessment_json,risk_json,execution_json) VALUES(?,?,?,?,?,?,?,?)',(
            _now(),symbol,int(candle_start_ms),str(assessment.get('action','hold')),float(assessment.get('confidence',0)),json.dumps(assessment,ensure_ascii=False),json.dumps(risk,ensure_ascii=False),json.dumps(execution,ensure_ascii=False)))
        return int(cur.lastrowid)


def get_state(key: str, default: str = '') -> str:
    with _connect() as conn:
        row=conn.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
    return str(row[0]) if row else default


def set_state(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value)))


def get_paper_position(symbol: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row=conn.execute("SELECT * FROM paper_positions WHERE symbol=? AND status='open'",(symbol.upper(),)).fetchone()
    return dict(row) if row else None


def open_paper_position(symbol: str, side: str, qty: float, entry: float, stop: float, take_profit: float) -> dict[str, Any]:
    opened=_now()
    with _connect() as conn:
        conn.execute('INSERT OR REPLACE INTO paper_positions(symbol,side,qty,entry,stop,take_profit,opened_at,status,unrealized) VALUES(?,?,?,?,?,?,?,\'open\',0)',(symbol.upper(),side,qty,entry,stop,take_profit,opened))
    return {"symbol":symbol.upper(),"side":side,"qty":qty,"entry":entry,"stop":stop,"take_profit":take_profit,"opened_at":opened,"status":"open"}


def close_paper_position(symbol: str, exit_price: float, reason: str) -> dict[str, Any] | None:
    pos=get_paper_position(symbol)
    if not pos: return None
    direction=1.0 if pos['side']=='long' else -1.0
    pnl=(float(exit_price)-float(pos['entry']))*float(pos['qty'])*direction
    closed=_now()
    with _connect() as conn:
        conn.execute("UPDATE paper_positions SET status='closed', unrealized=? WHERE symbol=?",(pnl,symbol.upper()))
        conn.execute('INSERT INTO paper_trades(symbol,side,qty,entry,exit,pnl,reason,opened_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?)',(symbol.upper(),pos['side'],pos['qty'],pos['entry'],exit_price,pnl,reason,pos['opened_at'],closed))
    return {**pos,"exit":exit_price,"pnl":pnl,"reason":reason,"closed_at":closed}


def update_paper_position(symbol: str, current_price: float) -> dict[str, Any] | None:
    pos=get_paper_position(symbol)
    if not pos: return None
    entry, stop, tp=float(pos['entry']),float(pos['stop']),float(pos['take_profit'])
    side=pos['side']
    reason=''
    if side=='long' and current_price <= stop: reason='stop_loss'
    elif side=='long' and current_price >= tp: reason='take_profit'
    elif side=='short' and current_price >= stop: reason='stop_loss'
    elif side=='short' and current_price <= tp: reason='take_profit'
    if reason: return close_paper_position(symbol,current_price,reason)
    direction=1.0 if side=='long' else -1.0
    unrealized=(current_price-entry)*float(pos['qty'])*direction
    with _connect() as conn:
        conn.execute('UPDATE paper_positions SET unrealized=? WHERE symbol=?',(unrealized,symbol.upper()))
    return {**pos,"unrealized":unrealized}


def paper_daily_pnl() -> float:
    day=datetime.now(timezone.utc).date().isoformat()
    with _connect() as conn:
        row=conn.execute("SELECT COALESCE(SUM(pnl),0) FROM paper_trades WHERE substr(closed_at,1,10)=?",(day,)).fetchone()
    return float(row[0] or 0)


def recent_assessments(limit: int = 20) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows=conn.execute('SELECT * FROM assessments ORDER BY id DESC LIMIT ?',(max(1,min(limit,200)),)).fetchall()
    out=[]
    for r in rows:
        d=dict(r)
        for key in ('assessment_json','risk_json','execution_json'):
            try: d[key[:-5]]=json.loads(d.pop(key))
            except Exception: pass
        out.append(d)
    return out


def recent_paper_trades(limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows=conn.execute('SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?',(max(1,min(limit,500)),)).fetchall()
    return [dict(r) for r in rows]
