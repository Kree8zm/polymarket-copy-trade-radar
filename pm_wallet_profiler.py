"""
pm_wallet_profiler.py — Phase 1: Read-Only Polymarket Wallet Profiler

Scans candidate wallets, computes *copyability* (realized PnL, consistency,
edge-decay, held-out validation, optional calibration) with hard guardrails
baked in, and writes a ranked JSON + text report + standalone HTML viewer.

READ-ONLY. No trades. No auto-updates. No execution.

Run:
    python3 pm_wallet_profiler.py                  # seed + auto-discovery
    python3 pm_wallet_profiler.py --seed-only       # seed list only
    python3 pm_wallet_profiler.py --auto-n 100

Guardrails (mapped from the user's 9-point spec — see plan doc):
  #1 realized vs unrealized PnL   -> score uses REALIZED pnl only
  #2 naive ROI misleading         -> score uses consistency, not ROI%
  #3 edge decay after detection   -> recent-vs-older performance penalty
  #4 no future-information leak   -> trades scored at their own timestamp;
                                      resolution data used only for calibration
  #6 paper overstatement          -> N/A in Phase 1 (no paper engine yet)
  #9 auto-overfit / no guardrails -> MIN_SAMPLE gate, held-out 70/30 split,
                                      observe-only (no trading, ever, here)
"""

from __future__ import annotations

import json
import os
import math
import statistics
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import pm_store  # Phase 2: SQLite persistence (zero-dep)
import pm_paper  # Phase 3: paper trading engine (zero-dep)
import sqlite3
import requests

# --------------------------------------------------------------------------
# Config / constants
# --------------------------------------------------------------------------

DATA_API_TRADES = "https://data-api.polymarket.com/trades"
DATA_API_ACTIVITY = "https://data-api.polymarket.com/activity"
GAMMA_API_MARKET = "https://gamma-api.polymarket.com/markets/{condition_id}"

MIN_SAMPLE = 50          # guardrail #9: min closed trades before a wallet is scored
MIN_NOTIONAL = 500.0     # guardrail for auto-discovery: ignore dust trades
MIN_RESOLVED_FOR_CALIBRATION = 20
MIN_EDGE_DECAY = 0.5     # hard floor: below this, reject regardless of score.
                         # (the soft log-scaled penalty alone let a wallet at
                         # 0.26x/0.10x of its earlier performance still pass
                         # on raw PnL size — that's backwards; severe decay
                         # should not be rescuable by a big score.)

# Strategy tuning (Bitcoin UP/DOWN playbook heuristics)
STRAT_HOLD_SHORT_S = 1 * 3600          # <1h => ultra-short penalty
STRAT_HOLD_SWEET_S = 4 * 3600          # 4h => sweet-spot bonus start
STRAT_HOLD_LONG_S = 24 * 3600          # >24h => long-horizon bonus
STRAT_CALIBRATION_BONUS = 0.10         # +10% if hit rate strong
STRAT_CALIBRATION_PENALTY = -0.10      # -10% if hit rate weak
STRAT_FOCUS_BONUS = 0.05               # +5% if focused specialist
STRAT_SCATTER_PENALTY = -0.05          # -5% if scattered across too many markets
STRAT_MARKET_FOCUS_RATIO = 0.25        # markets/trades below this = focused

OUTPUT_JSON = "copyable_wallets.json"
OUTPUT_HTML = os.environ.get("PM_OUTPUT_HTML", "copyable_wallets.html")

DISCLAIMER = (
    "Read-only research output. Not financial advice. Past performance on "
    "a public ledger is not evidence of future results; this report has not "
    "validated wallets for live copy-trading (see Phase 2/3)."
)

# User to populate from polymarket.com/leaderboard (copy addresses in manually —
# there is no public /leaderboard API endpoint as of this writing; gamma-api's
# /leaderboard returns 404). Auto-discovery (Task 2) supplements this list.
SEED_WALLETS: list[str] = [
    # "0x56687bf447db6ffa42ffe2204a05edaa20f55839",
]


# --------------------------------------------------------------------------
# Task 1: Data access layer
# --------------------------------------------------------------------------

def fetch_user_trades(
    wallet: str,
    limit: int = 10000,
    start: int = 0,
    taker_only: bool = True,
    session: Optional[requests.Session] = None,
) -> list[dict]:
    """All taker trades for a wallet. start=0 -> ~3y history.

    Paginates in batches of <=1000 (the API's practical page size). Any
    network/HTTP failure truncates the result rather than raising, so a
    single flaky wallet doesn't kill a whole scan run.
    """
    http = session or requests
    rows: list[dict] = []
    offset = 0
    while len(rows) < limit:
        batch = min(1000, limit - len(rows))
        try:
            r = http.get(
                DATA_API_TRADES,
                params={
                    "user": wallet,
                    "limit": batch,
                    "offset": offset,
                    "takerOnly": str(taker_only).lower(),
                    "start": start,
                },
                timeout=25,
            )
            if not r.ok:
                print(f"[warn] trades fetch HTTP {r.status_code} for {wallet}", file=sys.stderr)
                break
            data = r.json()
            if not data:
                break
            rows.extend(data)
            offset += len(data)
            if len(data) < batch:
                break
        except requests.RequestException as exc:
            print(f"[warn] trades fetch failed for {wallet}: {exc}", file=sys.stderr)
            break
    return rows


def fetch_user_activities(
    wallet: str,
    limit: int = 10000,
    start: int = 0,
    session: Optional[requests.Session] = None,
) -> list[dict]:
    """All wallet activities: REDEEM, SPLIT, MERGE, etc."""
    http = session or requests
    rows: list[dict] = []
    offset = 0
    while len(rows) < limit:
        batch = min(1000, limit - len(rows))
        try:
            r = http.get(
                DATA_API_ACTIVITY,
                params={
                    "user": wallet,
                    "limit": batch,
                    "offset": offset,
                    "start": start,
                },
                timeout=25,
            )
            if not r.ok:
                print(f"[warn] activity fetch HTTP {r.status_code} for {wallet}", file=sys.stderr)
                break
            data = r.json()
            if not data:
                break
            rows.extend(data)
            offset += len(data)
            if len(data) < batch:
                break
        except requests.RequestException as exc:
            print(f"[warn] activity fetch failed for {wallet}: {exc}", file=sys.stderr)
            break
    return rows


# --------------------------------------------------------------------------
# Task 2: Leaderboard scanner — candidate discovery
# --------------------------------------------------------------------------

def discover_candidates(
    auto_n: int = 50,
    min_notional: float = MIN_NOTIONAL,
    session: Optional[requests.Session] = None,
) -> list[str]:
    """Merge the hand-curated seed list with auto-discovered high-notional
    taker wallets from recent trade flow. There is no public leaderboard
    endpoint, so this is the practical substitute for Phase 1.
    """
    http = session or requests
    seen: list[str] = list(SEED_WALLETS)
    seen_set = set(seen)
    try:
        r = http.get(DATA_API_TRADES, params={"limit": 10000, "takerOnly": "true"}, timeout=30)
        if r.ok:
            notional = defaultdict(float)
            for t in r.json():
                w = t.get("proxyWallet")
                if not w:
                    continue
                try:
                    sz = float(t.get("size", 0))
                    px = float(t.get("price", 0))
                except (TypeError, ValueError):
                    continue
                if sz * px < min_notional:
                    continue
                notional[w] += sz * px
            top = sorted(notional.items(), key=lambda kv: kv[1], reverse=True)[:auto_n]
            for w, _ in top:
                if w not in seen_set:
                    seen.append(w)
                    seen_set.add(w)
        else:
            print(f"[warn] auto-discovery HTTP {r.status_code}", file=sys.stderr)
    except requests.RequestException as exc:
        print(f"[warn] auto-discovery failed: {exc}", file=sys.stderr)
    return seen


# --------------------------------------------------------------------------
# Task 3: Realized PnL accounting (FIFO per outcome token)
# --------------------------------------------------------------------------

def _token_key(t: dict) -> str:
    """Group by market outcome token (a wallet can hold both Yes and No
    lots on the same market independently)."""
    return f"{t.get('conditionId')}|{t.get('outcome')}"


def _fifo_match(trades: list[dict]):
    """Core FIFO matching engine. Returns (realized, open_pos, closed_events).

    closed_events is a list of dicts, one per matched fill:
        {key, matched_size, buy_price, sell_price, buy_ts, sell_ts, pnl}
    used by profile_wallet() for consistency/decay/hold-time metrics.
    A SELL with no open lot to match against (naked short in this feed,
    e.g. history truncated before our `start` window) is simply skipped —
    it contributes no realized PnL and does not raise.
    """
    lots: dict[str, deque] = defaultdict(deque)  # key -> deque of (size, price, ts) BUY lots
    realized = 0.0
    closed_events: list[dict] = []

    for t in sorted(trades, key=lambda x: x.get("timestamp", 0)):
        k = _token_key(t)
        try:
            size = float(t.get("size", 0))
            price = float(t.get("price", 0))
        except (TypeError, ValueError):
            continue
        ts = t.get("timestamp", 0)
        side = t.get("side")

        if side == "BUY":
            lots[k].append((size, price, ts))
        elif side == "SELL":
            remaining = size
            while remaining > 1e-9 and lots[k]:
                lot_size, lot_price, lot_ts = lots[k][0]
                matched = min(remaining, lot_size)
                pnl = (price - lot_price) * matched
                realized += pnl
                closed_events.append({
                    "key": k, "matched_size": matched,
                    "buy_price": lot_price, "sell_price": price,
                    "buy_ts": lot_ts, "sell_ts": ts, "pnl": pnl,
                })
                remaining -= matched
                if matched >= lot_size - 1e-9:
                    lots[k].popleft()
                else:
                    lots[k][0] = (lot_size - matched, lot_price, lot_ts)
            # else: naked short w.r.t. this data window — dropped, not crashed.

    open_pos = {
        k: {
            "size": sum(s for s, _, _ in v),
            "avg_cost": (sum(s * p for s, p, _ in v) / sum(s for s, _, _ in v)) if v else 0.0,
        }
        for k, v in lots.items() if v
    }
    return realized, open_pos, closed_events


def compute_realized_pnl(trades: list[dict]) -> tuple[float, dict]:
    """Public 2-tuple API used by the unit tests / held-out split.
    (realized_pnl, open_positions_by_token)
    """
    realized, open_pos, _ = _fifo_match(trades)
    return realized, open_pos


def filter_splits_merges(trades: list[dict]) -> list[dict]:
    """Drop SPLIT/MERGE activity rows so they don't distort directional metrics."""
    return [
        t for t in trades
        if t.get("side") in ("BUY", "SELL")
    ]


def apply_redeems(trades: list[dict], activities: list[dict]) -> tuple[list[dict], float]:
    """Apply REDEEM events to close remaining open positions at $1.00.
    Returns (updated closed_events, extra_realized_from_redemptions).
    """
    realized_from_redeems = 0.0
    redeem_events: list[dict] = []
    for a in activities:
        if a.get("type", "").strip().upper() != "REDEEM":
            continue
        cid = a.get("conditionId")
        outcome = a.get("outcome")
        ts = a.get("timestamp") or a.get("time") or 0
        try:
            size = float(a.get("size", 0))
        except (TypeError, ValueError):
            continue
        if not cid or not outcome or size <= 0:
            continue
        redeem_events.append({
            "key": f"{cid}|{outcome}",
            "size": size,
            "price": 1.0,
            "ts": ts,
        })

    if not redeem_events:
        return [], 0.0

    # Rebuild lots from original trades, then apply redeems
    lots: dict[str, deque] = defaultdict(deque)
    for t in sorted(trades, key=lambda x: x.get("timestamp", 0)):
        if t.get("side") != "BUY":
            continue
        k = _token_key(t)
        try:
            size = float(t.get("size", 0))
            price = float(t.get("price", 0))
        except (TypeError, ValueError):
            continue
        lots[k].append((size, price, t.get("timestamp", 0)))

    closed_from_redeems: list[dict] = []
    for r in sorted(redeem_events, key=lambda x: x["ts"]):
        k = r["key"]
        size = r["size"]
        price = r["price"]
        ts = r["ts"]
        remaining = size
        while remaining > 1e-9 and lots.get(k):
            lot_size, lot_price, lot_ts = lots[k][0]
            matched = min(remaining, lot_size)
            pnl = (price - lot_price) * matched
            realized_from_redeems += pnl
            closed_from_redeems.append({
                "key": k, "matched_size": matched,
                "buy_price": lot_price, "sell_price": price,
                "buy_ts": lot_ts, "sell_ts": ts, "pnl": pnl,
            })
            remaining -= matched
            if matched >= lot_size - 1e-9:
                lots[k].popleft()
            else:
                lots[k][0] = (lot_size - matched, lot_price, lot_ts)

    return closed_from_redeems, realized_from_redeems


# --------------------------------------------------------------------------
# Task 4: Wallet metrics aggregation
# --------------------------------------------------------------------------

@dataclass
class WalletProfile:
    wallet: str
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    n_trades: int = 0
    n_closed_trades: int = 0
    n_markets: int = 0
    total_volume: float = 0.0
    avg_trade_size: float = 0.0
    hold_period_median_s: float = 0.0
    edge_decay: float = 1.0          # recent/older realized ratio; <1 => decaying edge
    train_pnl: float = 0.0
    test_pnl: float = 0.0
    calibration: Optional[float] = None
    n_resolved: int = 0
    copyable: bool = False
    copy_score: float = 0.0
    strategy_score: float = 0.0
    trend_consistency: float = 0.0   # fraction of trades in dominant direction
    avg_entry_price: float = 0.0     # average buy price
    up_bias: float = 0.0            # fraction of buy-side exposure that was "up"/bullish
    reason: str = ""


def _edge_decay_ratio(closed_events: list[dict]) -> float:
    """Split closed events at the median sell timestamp; compare recent vs
    older realized PnL. Guarded against divide-by-zero / all-losing splits —
    this is a heuristic, not a statistical test (see Task 6 for the rigorous
    held-out check)."""
    if len(closed_events) < 4:
        return 1.0
    events = sorted(closed_events, key=lambda e: e["sell_ts"])
    mid = len(events) // 2
    older, recent = events[:mid], events[mid:]
    older_pnl = sum(e["pnl"] for e in older)
    recent_pnl = sum(e["pnl"] for e in recent)
    if older_pnl > 0:
        return recent_pnl / older_pnl
    if recent_pnl > 0:
        return 1.5  # went from non-positive to positive: not decaying
    return 1.0  # both non-positive: can't distinguish decay from noise


def profile_wallet(wallet: str, trades: list[dict], session: Optional[requests.Session] = None) -> WalletProfile:
    if not trades:
        return WalletProfile(wallet=wallet, reason="no trade history")

    # Filter out SPLIT/MERGE noise
    trades = filter_splits_merges(trades)
    if not trades:
        return WalletProfile(wallet=wallet, reason="no directional trades after split/merge filter")

    # Fetch activities for REDEEM handling
    activities = fetch_user_activities(wallet, session=session)

    realized, open_pos, closed_events = _fifo_match(trades)
    redeem_closed, redeem_realized = apply_redeems(trades, activities)
    closed_events.extend(redeem_closed)
    realized += redeem_realized

    volumes = []
    markets = set()
    for t in trades:
        try:
            sz = float(t.get("size", 0))
            px = float(t.get("price", 0))
            volumes.append(sz * px)
        except (TypeError, ValueError):
            continue
        markets.add(t.get("conditionId"))

    hold_periods = [e["sell_ts"] - e["buy_ts"] for e in closed_events if e["sell_ts"] >= e["buy_ts"]]

    # Trend consistency: fraction of buy-side outcome exposure in the dominant direction
    outcome_counts = defaultdict(int)
    entry_prices = []
    for t in trades:
        side = t.get("side")
        outcome = t.get("outcome")
        if side == "BUY" and outcome:
            outcome_counts[outcome] += 1
            try:
                entry_prices.append(float(t.get("price", 0)))
            except (TypeError, ValueError):
                pass

    total_outcome_buys = sum(outcome_counts.values())
    dominant_count = max(outcome_counts.values()) if outcome_counts else 0
    trend_consistency = dominant_count / total_outcome_buys if total_outcome_buys > 0 else 0.0
    avg_entry_price = sum(entry_prices) / len(entry_prices) if entry_prices else 0.0
    up_bias = sum(v for k, v in outcome_counts.items() if "up" in str(k).lower()) / total_outcome_buys if total_outcome_buys > 0 else 0.0

    train_pnl, test_pnl = heldout_check(trades)

    p = WalletProfile(
        wallet=wallet,
        realized_pnl=round(realized, 4),
        unrealized_pnl=round(_estimate_unrealized(open_pos, session), 4),
        n_trades=len(trades),
        n_closed_trades=len(closed_events),
        n_markets=len(markets),
        total_volume=round(sum(volumes), 2),
        avg_trade_size=round(sum(volumes) / len(volumes), 2) if volumes else 0.0,
        hold_period_median_s=statistics.median(hold_periods) if hold_periods else 0.0,
        edge_decay=round(_edge_decay_ratio(closed_events), 4),
        train_pnl=round(train_pnl, 4),
        test_pnl=round(test_pnl, 4),
        trend_consistency=round(trend_consistency, 4),
        avg_entry_price=round(avg_entry_price, 4),
        up_bias=round(up_bias, 4),
    )
    return p


def _estimate_unrealized(open_pos: dict, session: Optional[requests.Session]) -> float:
    """Best-effort mark-to-market of open positions using each market's
    latest price. Never used in scoring (guardrail #1) — display only.
    Returns 0.0 silently if the price lookup isn't available; this is
    explicitly NOT wired to a live endpoint in Phase 1 to avoid an extra
    N-market fetch per wallet during scans. Left as a documented no-op
    hook for Phase 2, where current price is already cached from the
    market-scanning layer (pm_intel_rank.py) instead of re-fetched here.
    """
    return 0.0


# --------------------------------------------------------------------------
# Task 6: Held-out validation (guardrail #9)
# --------------------------------------------------------------------------

def heldout_check(trades: list[dict]) -> tuple[float, float]:
    """Split trades by time: first 70% train, last 30% test.
    Returns (train_pnl, test_pnl). A wallet whose edge only shows up in the
    train split and vanishes in the test split is likely in-sample luck,
    not a repeatable edge — that's what score_copyability() gates on.
    """
    ts = sorted(trades, key=lambda x: x.get("timestamp", 0))
    if len(ts) < 10:
        # too few trades to split meaningfully; treat everything as "test"
        # so it can't pass the held-out gate by accident (guardrail #9).
        pnl, _ = compute_realized_pnl(ts)
        return 0.0, pnl
    cut = int(len(ts) * 0.7)
    train_pnl, _ = compute_realized_pnl(ts[:cut])
    test_pnl, _ = compute_realized_pnl(ts[cut:])
    return train_pnl, test_pnl


# --------------------------------------------------------------------------
# Task 5: Copyability score + guardrails
# --------------------------------------------------------------------------

def compute_strategy_bonus(p: WalletProfile) -> float:
    """Heuristic strategy bonus inspired by the Bitcoin UP/DOWN playbook:
    - Time-frame discipline: reward 4h-24h holds, penalize <1h flips
    - Calibration: reward hit-rate >60%, penalize <50%
    - Market focus: reward specialists, penalize scattered generalists
    - Trend consistency: reward wallets that stay directional
    - Entry pricing: reward buying cheap shares (<40c), penalize overpaying (>70c)
    Returns a multiplier in roughly [0.75, 1.20].
    """
    bonus = 0.0

    # Time-frame discipline via median hold period
    hold_s = p.hold_period_median_s
    if hold_s <= 0:
        # no hold data => neutral
        pass
    elif hold_s < STRAT_HOLD_SHORT_S:
        bonus += STRAT_CALIBRATION_PENALTY  # ultra-short
    elif hold_s < STRAT_HOLD_SWEET_S:
        pass  # short but not ultra-short => neutral
    elif hold_s <= STRAT_HOLD_LONG_S:
        bonus += STRAT_CALIBRATION_BONUS   # 4h-24h sweet spot
    else:
        bonus += STRAT_FOCUS_BONUS          # >24h long-horizon

    # Calibration / hit rate
    if p.calibration is not None and p.n_resolved >= MIN_RESOLVED_FOR_CALIBRATION:
        if p.calibration >= 0.60:
            bonus += STRAT_CALIBRATION_BONUS
        elif p.calibration < 0.50:
            bonus += STRAT_CALIBRATION_PENALTY

    # Market focus vs scatter
    if p.n_closed_trades > 0:
        focus_ratio = p.n_markets / p.n_closed_trades
        if focus_ratio <= STRAT_MARKET_FOCUS_RATIO:
            bonus += STRAT_FOCUS_BONUS
        elif focus_ratio >= 0.75:
            bonus += STRAT_SCATTER_PENALTY

    # Trend consistency: directional discipline
    if p.trend_consistency >= 0.80:
        bonus += STRAT_FOCUS_BONUS
    elif p.trend_consistency <= 0.55:
        bonus += STRAT_SCATTER_PENALTY

    # Entry pricing discipline
    if 0.0 < p.avg_entry_price < 0.40:
        bonus += STRAT_FOCUS_BONUS
    elif p.avg_entry_price > 0.70:
        bonus += STRAT_CALIBRATION_PENALTY

    # Clamp to sane multiplier band
    return max(0.75, min(1.20, 1.0 + bonus))


def score_copyability(p: WalletProfile) -> WalletProfile:
    # Guardrail #9: min sample gate — exclude tiny/single-bet wallets.
    if p.n_closed_trades < MIN_SAMPLE:
        p.copyable = False
        p.copy_score = 0.0
        p.reason = f"insufficient sample ({p.n_closed_trades}<{MIN_SAMPLE})"
        return p

    # Guardrail #9: held-out gate — edge must survive out-of-sample.
    if p.test_pnl <= 0:
        p.copyable = False
        p.copy_score = 0.0
        p.reason = "edge fails out-of-sample (held-out PnL <= 0)"
        return p

    # Hard floor: severe decay is disqualifying on its own, independent of
    # how large realized_pnl is. (A wallet that made $100k while decaying
    # to 10% of its earlier edge is not the same as a wallet still at 100%.)
    if p.edge_decay < MIN_EDGE_DECAY:
        p.copyable = False
        p.copy_score = 0.0
        p.reason = f"edge decay too severe (recent {p.edge_decay:.2f}x of older, floor is {MIN_EDGE_DECAY})"
        return p

    # Guardrail #1/#2: score on realized PnL + consistency, NOT ROI%.
    if p.realized_pnl <= 0:
        p.copyable = False
        p.copy_score = 0.0
        p.reason = "no positive realized edge"
        return p

    base = math.log1p(p.realized_pnl)
    consistency = min(p.n_closed_trades / 200.0, 1.0)

    # Guardrail #3: soft penalty for decay above the hard floor but still
    # below the "stable" threshold (0.8) — same log-scaled shape as before,
    # now only reachable once the hard floor above has already been cleared.
    decay_penalty = 0.0
    if p.edge_decay < 0.8:
        decay_penalty = min(0.4 * (0.8 - p.edge_decay), 1.0)

    score = round(base * consistency * (1.0 - decay_penalty), 4)

    if decay_penalty >= 0.4:
        p.copyable = False
        p.copy_score = 0.0
        p.reason = f"edge decay too high (recent {p.edge_decay:.2f}x of older)"
    else:
        p.copyable = True
        p.strategy_score = round(compute_strategy_bonus(p), 4)
        p.copy_score = round(score * p.strategy_score, 4)
        p.reason = "realized PnL + consistency; edge stable and held out"
    return p


# --------------------------------------------------------------------------
# Task 7: Calibration (refinement, gated — never blocks core scoring)
# --------------------------------------------------------------------------

def fetch_resolution(condition_id: str, session: Optional[requests.Session] = None,
                      cache: Optional[dict] = None) -> Optional[dict]:
    """Best-effort per-market resolution lookup. Returns None on any
    failure or unexpected shape — calibration is explicitly allowed to be
    unavailable (see Task 7 in the plan)."""
    if cache is not None and condition_id in cache:
        return cache[condition_id]
    http = session or requests
    result = None
    try:
        r = http.get(GAMMA_API_MARKET.format(condition_id=condition_id), timeout=15)
        if r.ok:
            data = r.json()
            if isinstance(data, dict) and data.get("resolved"):
                result = data
    except requests.RequestException:
        result = None
    if cache is not None:
        cache[condition_id] = result
    return result


def calibration(trades: list[dict], session: Optional[requests.Session] = None) -> tuple[Optional[float], int]:
    """Hit-rate: across resolved markets the wallet traded, was its net
    position on the winning outcome? Returns (hit_rate_or_None, n_resolved).
    Requires MIN_RESOLVED_FOR_CALIBRATION resolved markets or returns None —
    a small sample here is not evidence of skill either way.
    """
    net_by_market: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    condition_ids = set()
    for t in trades:
        cid = t.get("conditionId")
        outcome = t.get("outcome")
        if not cid or not outcome:
            continue
        try:
            sz = float(t.get("size", 0))
        except (TypeError, ValueError):
            continue
        sign = 1.0 if t.get("side") == "BUY" else -1.0
        net_by_market[cid][outcome] += sign * sz
        condition_ids.add(cid)

    cache: dict = {}
    correct = 0
    n_resolved = 0
    for cid in condition_ids:
        res = fetch_resolution(cid, session=session, cache=cache)
        if not res:
            continue
        outcomes = net_by_market[cid]
        if not outcomes:
            continue
        predicted_outcome = max(outcomes, key=lambda o: outcomes[o])
        if outcomes[predicted_outcome] <= 0:
            continue  # net flat or net short everything — no directional call
        winning_outcome = res.get("winningOutcome") or res.get("outcome")
        if winning_outcome is None:
            continue
        n_resolved += 1
        if str(predicted_outcome).strip().lower() == str(winning_outcome).strip().lower():
            correct += 1

    if n_resolved < MIN_RESOLVED_FOR_CALIBRATION:
        return None, n_resolved
    return round(correct / n_resolved, 4), n_resolved


# --------------------------------------------------------------------------
# Task 8: Output — JSON + text report + standalone HTML
# --------------------------------------------------------------------------

def write_json(profiles: list[WalletProfile], path: str = OUTPUT_JSON) -> None:
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "disclaimer": DISCLAIMER,
        "wallets": [asdict(p) for p in profiles],
    }
    Path(path).write_text(json.dumps(payload, indent=2))


def print_report(profiles: list[WalletProfile], top_n: int = 20) -> None:
    copyable = [p for p in profiles if p.copyable]
    print(f"\n=== Polymarket Wallet Profiler — {len(profiles)} scanned, {len(copyable)} copyable ===")
    print(DISCLAIMER)
    print()
    for i, p in enumerate(copyable[:top_n], 1):
        print(f"#{i:<3} {p.wallet}")
        print(f"      score={p.copy_score:<8} realized_pnl=${p.realized_pnl:<10,.2f} "
              f"closed_trades={p.n_closed_trades:<5} markets={p.n_markets:<5} "
              f"edge_decay={p.edge_decay}")
        cal = f"{p.calibration:.1%} (n={p.n_resolved})" if p.calibration is not None else "n/a"
        print(f"      held_out_test_pnl=${p.test_pnl:,.2f}  calibration={cal}  reason={p.reason}")
    if not copyable:
        print("(no wallets cleared the guardrails this run)")
    excluded = len(profiles) - len(copyable)
    if excluded:
        print(f"\n{excluded} wallet(s) excluded by guardrails (see JSON for reasons).")


def write_html(profiles: list[WalletProfile], path: str = OUTPUT_HTML) -> None:
    ranked = sorted(profiles, key=lambda p: p.copy_score, reverse=True)
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    copyable = [p for p in ranked if p.copyable]
    excluded = [p for p in ranked if not p.copyable]

    def short_addr(addr: str) -> str:
        return addr[:8] + "..." + addr[-4:] if len(addr) > 12 else addr

    def badge_for(p: WalletProfile):
        if p.edge_decay < 0.5:
            return ("badge-red", f"● Strategy Fading ({p.copy_score:.2f}x)")
        if p.copy_score > 1.0:
            return ("badge-green", f"● Top Tier ({p.copy_score:.2f}x)")
        if p.copy_score >= 0.8:
            return ("badge-yellow", f"● Caution ({p.copy_score:.2f}x)")
        return ("badge-red", f"● Low Consistency ({p.copy_score:.2f}x)")

    def categories_for(p: WalletProfile) -> str:
        tags = []
        if p.edge_decay >= 0.8:
            tags.append("safe")
        if p.copy_score >= 0.8:
            tags.append("discipline")
        if p.realized_pnl > 0:
            tags.append("profit")
        if p.avg_entry_price > 0 and p.avg_entry_price < 0.40:
            tags.append("discipline")
        return " ".join(tags) or "safe"

    trader_cards = []
    for i, p in enumerate(ranked, 1):
        is_top = i == 1
        held_out = p.test_pnl if hasattr(p, 'test_pnl') else getattr(p, 'held_out_pnl', 0.0)
        badge_cls, badge_txt = badge_for(p)
        cats = categories_for(p)
        up_pct = p.up_bias * 100
        down_pct = 100 - up_pct
        warning_html = ""
        if p.edge_decay < 0.5:
            warning_html = f"""
      <div class="warning-banner">
        ⚠️ Strategy Degrading (Edge Decay {p.edge_decay:.2f}x)
      </div>"""
        trader_cards.append(f"""
    <div class="wallet-card{' top-pick' if is_top else ''}"
         data-category="{cats}"
         data-pnl="{p.realized_pnl:,.0f}"
         data-entry="{p.avg_entry_price:.2f}"
         data-decay="{p.edge_decay:.4f}">
      <div class="card-header">
        <div class="wallet-id">
          <span class="rank">#{i}</span>
          <span class="address">{short_addr(p.wallet)}</span>
        </div>
        <span class="score-badge {badge_cls}">{badge_txt}</span>
      </div>{warning_html}
      <div class="stats-grid">
        <div class="stat-item">
          <span class="stat-label">Realized Profit</span>
          <span class="stat-value" style="color: var(--green);">+${p.realized_pnl:,.0f}</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">Avg Entry Price
            <span class="tooltip" data-tip="Average cost per share. Under $0.40 limits downside risk.">ⓘ</span>
          </span>
          <span class="stat-value">${p.avg_entry_price:.2f}</span>
        </div>
      </div>
      <div class="bias-container">
        <div class="bias-header">
          <span>Trade Bias</span>
          <span>{up_pct:.0f}% UP / {down_pct:.0f}% DOWN</span>
        </div>
        <div class="bias-bar">
          <div class="bias-up" style="width: {up_pct:.0f}%;"></div>
          <div class="bias-down" style="width: {down_pct:.0f}%;"></div>
        </div>
      </div>
      <div class="actions-row">
        <button class="btn btn-primary" onclick="copyAddress('{p.wallet}')">
          📋 Copy Address
        </button>
        <a href="https://polymarket.com/profile/{p.wallet}" target="_blank" class="btn btn-secondary">
          Inspect ↗
        </a>
      </div>
      <button class="accordion-trigger" onclick="toggleAccordion(this)">
        View Detailed Analysis ▾
      </button>
      <div class="accordion-content">
        <div class="stats-grid">
          <div class="stat-item">
            <span class="stat-label">Held-out PnL (30%)
              <span class="tooltip" data-tip="Out-of-sample profit on final 30% of trades to prevent curve-fitting.">ⓘ</span>
            </span>
            <span class="stat-value">+${held_out:,.0f}</span>
          </div>
          <div class="stat-item">
            <span class="stat-label">Strategy Multiplier
              <span class="tooltip" data-tip="Playbook bonus score based on entry prices and hold duration.">ⓘ</span>
            </span>
            <span class="stat-value">{p.strategy_score:.2f}x</span>
          </div>
        </div>
        <div class="stats-grid">
          <div class="stat-item">
            <span class="stat-label">Closed Trades</span>
            <span class="stat-value">{p.n_closed_trades}</span>
          </div>
          <div class="stat-item">
            <span class="stat-label">Strategy Multiplier</span>
            <span class="stat-value">{p.strategy_score:.2f}x</span>
          </div>
        </div>
      </div>
    </div>""")

    excluded_items = []
    for p in excluded:
        reason = getattr(p, 'last_reason', '') or p.reason or "Excluded"
        excluded_items.append(f"""
        <div class="excluded-item">
          <span style="font-family: monospace;">{short_addr(p.wallet)}</span>
          <span class="excluded-reason">{reason}</span>
        </div>""")

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Polymarket Copy-Trade Radar</title>
  <!-- Telegram WebApp SDK -->
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {{
      --bg-color: var(--tg-theme-bg-color, #121214);
      --card-bg: var(--tg-theme-secondary-bg-color, #1c1c1e);
      --text-color: var(--tg-theme-text-color, #f2f2f7);
      --hint-color: var(--tg-theme-hint-color, #8e8e93);
      --accent-color: var(--tg-theme-button-color, #007aff);
      --accent-text: var(--tg-theme-button-text-color, #ffffff);
      --green: #30d158;
      --yellow: #ffd60a;
      --red: #ff453a;
      --border-color: rgba(255, 255, 255, 0.08);
    }}

    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }}

    body {{
      background-color: var(--bg-color);
      color: var(--text-color);
      padding: 12px;
      max-width: 600px;
      margin: 0 auto;
      padding-bottom: 40px;
    }}

    header {{
      margin-bottom: 16px;
    }}

    header h1 {{
      font-size: 20px;
      font-weight: 700;
    }}

    header p {{
      font-size: 13px;
      color: var(--hint-color);
      margin-top: 2px;
    }}

    .filter-pills {{
      display: flex;
      gap: 8px;
      overflow-x: auto;
      padding-bottom: 8px;
      margin-bottom: 16px;
      scrollbar-width: none;
    }}

    .filter-pills::-webkit-scrollbar {{
      display: none;
    }}

    .pill {{
      background: var(--card-bg);
      color: var(--hint-color);
      border: 1px solid var(--border-color);
      padding: 6px 14px;
      border-radius: 20px;
      font-size: 13px;
      font-weight: 500;
      white-space: nowrap;
      cursor: pointer;
      transition: all 0.2s ease;
    }}

    .pill.active {{
      background: var(--accent-color);
      color: var(--accent-text);
      border-color: var(--accent-color);
    }}

    .wallet-card {{
      background: var(--card-bg);
      border: 1px solid var(--border-color);
      border-radius: 14px;
      padding: 14px;
      margin-bottom: 12px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}

    .wallet-card.top-pick {{
      border-color: rgba(48, 209, 88, 0.4);
      background: linear-gradient(180deg, rgba(48, 209, 88, 0.08) 0%, var(--card-bg) 100%);
    }}

    .card-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}

    .wallet-id {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    .rank {{
      font-size: 12px;
      font-weight: 700;
      color: var(--hint-color);
      background: rgba(255, 255, 255, 0.05);
      padding: 2px 6px;
      border-radius: 4px;
    }}

    .address {{
      font-size: 14px;
      font-weight: 600;
      font-family: ui-monospace, monospace;
    }}

    .score-badge {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 4px 10px;
      border-radius: 12px;
      font-size: 12px;
      font-weight: 600;
    }}

    .badge-green {{ background: rgba(48, 209, 88, 0.15); color: var(--green); }}
    .badge-yellow {{ background: rgba(255, 214, 10, 0.15); color: var(--yellow); }}
    .badge-red {{ background: rgba(255, 69, 58, 0.15); color: var(--red); }}

    .stats-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      background: rgba(0, 0, 0, 0.2);
      padding: 10px;
      border-radius: 10px;
    }}

    .stat-item {{
      display: flex;
      flex-direction: column;
    }}

    .stat-label {{
      font-size: 11px;
      color: var(--hint-color);
      display: flex;
      align-items: center;
      gap: 4px;
    }}

    .stat-value {{
      font-size: 15px;
      font-weight: 700;
      margin-top: 2px;
    }}

    .tooltip {{
      position: relative;
      cursor: pointer;
      display: inline-block;
    }}

    .tooltip::after {{
      content: attr(data-tip);
      position: absolute;
      bottom: 130%;
      left: 50%;
      transform: translateX(-50%);
      background: #2c2c2e;
      color: #ffffff;
      padding: 6px 10px;
      font-size: 11px;
      font-weight: 400;
      border-radius: 6px;
      white-space: normal;
      width: 180px;
      text-align: center;
      box-shadow: 0 4px 12px rgba(0,0,0,0.3);
      opacity: 0;
      visibility: hidden;
      transition: opacity 0.2s ease, visibility 0.2s ease;
      z-index: 100;
      pointer-events: none;
    }}

    .tooltip:hover::after, .tooltip:focus::after {{
      opacity: 1;
      visibility: visible;
    }}

    .bias-container {{
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}

    .bias-header {{
      display: flex;
      justify-content: space-between;
      font-size: 11px;
      color: var(--hint-color);
    }}

    .bias-bar {{
      height: 6px;
      width: 100%;
      background: rgba(255, 255, 255, 0.1);
      border-radius: 3px;
      overflow: hidden;
      display: flex;
    }}

    .bias-up {{ background: var(--green); height: 100%; }}
    .bias-down {{ background: var(--red); height: 100%; }}

    .actions-row {{
      display: flex;
      gap: 8px;
    }}

    .btn {{
      flex: 1;
      padding: 9px 0;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      border: none;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      text-decoration: none;
    }}

    .btn-primary {{
      background: var(--accent-color);
      color: var(--accent-text);
    }}

    .btn-secondary {{
      background: rgba(255, 255, 255, 0.08);
      color: var(--text-color);
    }}

    .accordion-trigger {{
      background: none;
      border: none;
      color: var(--hint-color);
      font-size: 12px;
      font-weight: 500;
      cursor: pointer;
      padding: 4px 0 0 0;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 4px;
    }}

    .accordion-content {{
      display: none;
      flex-direction: column;
      gap: 10px;
      padding-top: 10px;
      border-top: 1px dashed var(--border-color);
    }}

    .accordion-content.open {{
      display: flex;
    }}

    .warning-banner {{
      background: rgba(255, 69, 58, 0.12);
      border: 1px solid rgba(255, 69, 58, 0.3);
      color: var(--red);
      font-size: 11px;
      font-weight: 600;
      padding: 6px 10px;
      border-radius: 6px;
      display: flex;
      align-items: center;
      gap: 6px;
    }}

    .excluded-list {{
      margin-top: 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}

    .excluded-item {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 12px;
      padding: 8px;
      background: rgba(0,0,0,0.2);
      border-radius: 6px;
    }}

    .excluded-reason {{
      color: var(--red);
      font-size: 11px;
    }}

    .disclaimer {{
      font-size: 11px;
      color: var(--hint-color);
      text-align: center;
      line-height: 1.5;
    }}

    #toast {{
      position: fixed;
      bottom: 20px;
      left: 50%;
      transform: translateX(-50%) translateY(100px);
      background: rgba(44, 44, 46, 0.95);
      color: #fff;
      padding: 10px 18px;
      border-radius: 20px;
      font-size: 13px;
      font-weight: 500;
      box-shadow: 0 4px 12px rgba(0,0,0,0.4);
      transition: transform 0.3s ease;
      z-index: 1000;
      pointer-events: none;
    }}

    #toast.show {{
      transform: translateX(-50%) translateY(0);
    }}
  </style>
</head>
<body>

  <div class="container">
    
    <header>
      <h1>Copy-Trade Radar</h1>
      <p>Ranked Polymarket traders matching BTC UP/DOWN playbook rules.</p>
    </header>

    <div class="filter-pills">
      <button class="pill active" data-filter="all">All Top</button>
      <button class="pill" data-filter="safe">🎯 Safe & Consistent</button>
      <button class="pill" data-filter="profit">💰 Top Profit</button>
      <button class="pill" data-filter="discipline">⚡ Entry &lt; $0.40</button>
    </div>

    <div id="wallets-container">
      {''.join(trader_cards)}
    </div>

    <details>
      <summary>
        <span>⚠️ Filtered / Excluded Wallets ({len(excluded)})</span>
        <span style="font-size: 11px;">Tap to view</span>
      </summary>
      <div class="excluded-list">
        {''.join(excluded_items)}
      </div>
    </details>

    <div class="disclaimer">
      Read-only research report. Not financial advice. Always verify on Polymarket before copying live trades.
    </div>

  </div>

  <!-- Toast Element -->
  <div id="toast">Address copied to clipboard!</div>

  <script>
    // Initialize Telegram WebApp SDK
    const tg = window.Telegram?.WebApp;
    if (tg) {{
      tg.ready();
      tg.expand();
    }}

    // Filter Pills Logic using data-category
    const filterPills = document.querySelectorAll('.pill');
    const walletCards = document.querySelectorAll('.wallet-card');

    filterPills.forEach(pill => {{
      pill.addEventListener('click', () => {{
        filterPills.forEach(p => p.classList.remove('active'));
        pill.classList.add('active');
        const filter = pill.getAttribute('data-filter');
        walletCards.forEach(card => {{
          if (filter === 'all') {{
            card.style.display = 'flex';
          }} else {{
            const cats = card.getAttribute('data-category') || '';
            card.style.display = cats.includes(filter) ? 'flex' : 'none';
          }}
        }});
        if (tg?.HapticFeedback) {{
          tg.HapticFeedback.selectionChanged();
        }}
      }});
    }});

    // Copy Address + Telegram Haptic & Toast
    function copyAddress(address) {{
      navigator.clipboard.writeText(address).then(() => {{
        if (tg?.HapticFeedback) {{
          tg.HapticFeedback.impactOccurred('light');
        }}
        showToast("Address copied to clipboard!");
      }}).catch(err => {{
        console.error('Copy failed', err);
      }});
    }}

    function showToast(message) {{
      const toast = document.getElementById('toast');
      toast.textContent = message;
      toast.classList.add('show');
      setTimeout(() => {{
        toast.classList.remove('show');
      }}, 2000);
    }}

    // Accordion Toggle Logic
    function toggleAccordion(button) {{
      const content = button.nextElementSibling;
      const isOpen = content.classList.contains('open');
      if (isOpen) {{
        content.classList.remove('open');
        button.textContent = 'View Detailed Analysis ▾';
      }} else {{
        content.classList.add('open');
        button.textContent = 'Hide Detailed Analysis ▴';
        if (tg?.HapticFeedback) {{
          tg.HapticFeedback.impactOccurred('soft');
        }}
      }}
    }}
  </script>
</body>
</html>"""
    Path(path).write_text(html)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run(seed_only: bool = False, auto_n: int = 50, use_calibration: bool = True,
        paper_enabled: bool = True) -> list[WalletProfile]:
    session = requests.Session()
    candidates = list(SEED_WALLETS) if seed_only else discover_candidates(auto_n, session=session)

    if not candidates:
        print("[warn] no candidate wallets — populate SEED_WALLETS or check network access.", file=sys.stderr)

    profiles: list[WalletProfile] = []
    for w in candidates:
        cached = pm_store.get_cached_trades(w)
        if cached is not None:
            trades = cached
            print(f"[cache] using cached trades for {w}", file=sys.stderr)
        else:
            trades = fetch_user_trades(w, session=session)
            if trades:
                pm_store.set_cached_trades(w, trades)
        if not trades:
            continue
        p = profile_wallet(w, trades, session=session)
        p = score_copyability(p)
        if use_calibration:
            hit_rate, n_resolved = calibration(trades, session=session)
            p.calibration = hit_rate
            p.n_resolved = n_resolved
        profiles.append(p)
        time.sleep(0.1)  # be polite to the API between wallets

    profiles.sort(key=lambda p: p.copy_score, reverse=True)
    write_json(profiles)
    print_report(profiles)
    write_html(profiles)
    try:
        conn = sqlite3.connect(pm_store.DB_PATH)
        pm_store.init_db(conn)
        n = pm_store.persist_run(profiles, conn=conn,
                                 run_ts=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()))
        print(f"[db] persisted {n} wallets -> {pm_store.DB_PATH}", file=sys.stderr)
        if paper_enabled:
            summaries = pm_paper.run_paper_engine(conn, profiles, session=session)
            pm_paper.print_paper_report(summaries)
        conn.close()
    except Exception as exc:
        print(f"[warn] db/persist failed: {exc}", file=sys.stderr)
    return profiles


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-only", action="store_true", help="skip auto-discovery, use SEED_WALLETS only")
    parser.add_argument("--auto-n", type=int, default=50, help="number of auto-discovered wallets to add")
    parser.add_argument("--no-calibration", action="store_true", help="skip the per-market resolution lookup")
    parser.add_argument("--no-paper", action="store_true", help="skip the Phase 3 paper trading engine")
    args = parser.parse_args()
    run(seed_only=args.seed_only, auto_n=args.auto_n,
        use_calibration=not args.no_calibration, paper_enabled=not args.no_paper)
