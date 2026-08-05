#!/usr/bin/env python3
"""
pm_store.py — SQLite persistence for the Polymarket Wallet Profiler (Phase 2).

Right-sized replacement for the ephemeral JSON list. No pip, no server:
stdlib sqlite3 only. Tracks each wallet's metrics *over time* so we can see
an alpha trader losing their touch across runs (wallet_metrics_history) —
the core product differentiator from the productization doc.

Tables:
  wallets                — latest snapshot + status per address
  wallet_metrics_history — time series of metrics per wallet (decay tracking)
  copy_decisions         — scaffold for Phase 3 journal (paper/live/canary)
  wallet_cache           — raw trade cache with TTL
"""

from __future__ import annotations

import sqlite3
import os
import time
import json
from dataclasses import dataclass
from typing import Optional

DB_PATH = os.environ.get("PM_DB_PATH", "copyable_wallets.db")
WALLET_CACHE_TTL = 900  # 15 minutes


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS wallets (
            wallet          TEXT PRIMARY KEY,
            realized_pnl    REAL,
            n_closed_trades INTEGER,
            n_markets       INTEGER,
            total_volume    REAL,
            edge_decay      REAL,
            held_out_pnl    REAL,
            calibration     REAL,
            n_resolved      INTEGER,
            copy_score      REAL,
            strategy_score  REAL,
            trend_consistency REAL,
            avg_entry_price REAL,
            copyable        INTEGER,
            status          TEXT,
            last_reason     TEXT,
            last_scanned    TEXT
        );

        CREATE TABLE IF NOT EXISTS wallet_metrics_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet         TEXT,
            run_ts         TEXT,
            realized_pnl   REAL,
            copy_score     REAL,
            edge_decay     REAL,
            held_out_pnl   REAL,
            calibration    REAL,
            n_closed_trades INTEGER,
            copyable       INTEGER
        );

        CREATE TABLE IF NOT EXISTS copy_decisions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                  TEXT,
            target_wallet       TEXT,
            market_condition_id TEXT,
            outcome             TEXT,
            side                TEXT,
            target_price        REAL,
            execution_price     REAL,
            size                REAL,
            fee                 REAL,
            simulated_slippage  REAL,
            actual_slippage     REAL,
            status              TEXT,
            pnl                 REAL
        );

        CREATE TABLE IF NOT EXISTS wallet_cache (
            address     TEXT PRIMARY KEY,
            last_updated TEXT,
            trades      TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_history_wallet ON wallet_metrics_history(wallet);
        CREATE INDEX IF NOT EXISTS idx_decisions_wallet ON copy_decisions(target_wallet);
        """
    )
    # Migration for DBs created before the paper-engine columns existed.
    _cols = {r[1] for r in conn.execute("PRAGMA table_info(copy_decisions)").fetchall()}
    for col, typ in [
        ("side", "TEXT"), ("target_price", "REAL"), ("execution_price", "REAL"),
        ("size", "REAL"), ("fee", "REAL"),
    ]:
        if col not in _cols:
            conn.execute(f"ALTER TABLE copy_decisions ADD COLUMN {col} {typ}")


def _status_for(p) -> str:
    if not p.copyable:
        return "excluded"
    return "whitelisted"


def get_cached_trades(wallet: str, path: str = DB_PATH) -> Optional[list[dict]]:
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "SELECT last_updated, trades FROM wallet_cache WHERE address=?",
            (wallet,),
        )
        row = cur.fetchone()
        if not row:
            return None
        last_updated, trades_json = row
        if time.time() - float(last_updated) > WALLET_CACHE_TTL:
            return None
        return json.loads(trades_json) if trades_json else None
    finally:
        conn.close()


def set_cached_trades(wallet: str, trades: list[dict], path: str = DB_PATH) -> None:
    conn = sqlite3.connect(path)
    try:
        init_db(conn)
        conn.execute(
            "INSERT INTO wallet_cache (address, last_updated, trades) VALUES (?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET last_updated=excluded.last_updated, trades=excluded.trades",
            (wallet, str(time.time()), json.dumps(trades)),
        )
        conn.commit()
    finally:
        conn.close()


def persist_run(profiles: list, path: str = DB_PATH, conn=None,
                run_ts: Optional[str] = None) -> int:
    """Write a full scan's results: upsert wallets + append history rows.
    Returns number of wallets persisted. Safe to call from run(); any DB
    failure is raised so the caller can decide, but we wrap at the call site.
    Pass conn to reuse an open connection (e.g. for the paper engine)."""
    import datetime as dt
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(path)
    try:
        if run_ts is None:
            run_ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        init_db(conn)
        cur = conn.cursor()
        for p in profiles:
            cur.execute(
                """INSERT INTO wallets (
                       wallet, realized_pnl, n_closed_trades, n_markets, total_volume,
                       edge_decay, held_out_pnl, calibration, n_resolved, copy_score,
                       strategy_score, trend_consistency, avg_entry_price, copyable,
                       status, last_reason, last_scanned)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(wallet) DO UPDATE SET
                       realized_pnl=excluded.realized_pnl,
                       n_closed_trades=excluded.n_closed_trades,
                       n_markets=excluded.n_markets,
                       total_volume=excluded.total_volume,
                       edge_decay=excluded.edge_decay,
                       held_out_pnl=excluded.held_out_pnl,
                       calibration=excluded.calibration,
                       n_resolved=excluded.n_resolved,
                       copy_score=excluded.copy_score,
                       strategy_score=excluded.strategy_score,
                       trend_consistency=excluded.trend_consistency,
                       avg_entry_price=excluded.avg_entry_price,
                       copyable=excluded.copyable,
                       status=excluded.status,
                       last_reason=excluded.last_reason,
                       last_scanned=excluded.last_scanned""",
                (
                    p.wallet, getattr(p, "realized_pnl", 0.0),
                    getattr(p, "n_closed_trades", 0), getattr(p, "n_markets", 0),
                    getattr(p, "total_volume", 0.0), getattr(p, "edge_decay", 1.0),
                    getattr(p, "test_pnl", 0.0), getattr(p, "calibration", None),
                    getattr(p, "n_resolved", 0), getattr(p, "copy_score", 0.0),
                    getattr(p, "strategy_score", 0.0),
                    getattr(p, "trend_consistency", 0.0), getattr(p, "avg_entry_price", 0.0),
                    1 if p.copyable else 0, _status_for(p), p.reason, run_ts,
                ),
            )
            cur.execute(
                """INSERT INTO wallet_metrics_history (
                       wallet, run_ts, realized_pnl, copy_score, edge_decay,
                       held_out_pnl, calibration, n_closed_trades, copyable)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    p.wallet, run_ts, getattr(p, "realized_pnl", 0.0),
                    getattr(p, "copy_score", 0.0), getattr(p, "edge_decay", 1.0),
                    getattr(p, "test_pnl", 0.0), getattr(p, "calibration", None),
                    getattr(p, "n_closed_trades", 0), 1 if p.copyable else 0,
                ),
            )
        conn.commit()
        return len(profiles)
    finally:
        if own_conn:
            conn.close()


def get_latest_copyable(path: str = DB_PATH, limit: int = 10) -> list[dict]:
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "SELECT wallet, copy_score, realized_pnl, n_closed_trades, edge_decay, "
            "held_out_pnl, last_reason, last_scanned FROM wallets "
            "WHERE copyable=1 ORDER BY copy_score DESC LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def get_history(wallet: str, path: str = DB_PATH, limit: int = 30) -> list[dict]:
    """Time series of a wallet's metrics — used to detect decay over weeks."""
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "SELECT run_ts, realized_pnl, copy_score, edge_decay, held_out_pnl, "
            "calibration, copyable FROM wallet_metrics_history "
            "WHERE wallet=? ORDER BY run_ts ASC LIMIT ?",
            (wallet, limit),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def decay_trend(wallet: str, path: str = DB_PATH) -> Optional[float]:
    """Compare the wallet's most recent edge_decay to its first recorded one.
    Returns recent/first ratio (>1 = improving, <1 = decaying). None if <2 runs."""
    hist = get_history(wallet, path, limit=1000)
    decays = [h["edge_decay"] for h in hist if h["edge_decay"] is not None]
    if len(decays) < 2:
        return None
    return round(decays[-1] / decays[0], 3) if decays[0] else None


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    top = get_latest_copyable(db)
    print(f"DB: {db} | copyable now: {len(top)}")
    for w in top:
        print(f"  {w['wallet']}  score={w['copy_score']}  realized=${w['realized_pnl']:,.0f}  "
              f"decay={w['edge_decay']}  scanned={w['last_scanned']}")
