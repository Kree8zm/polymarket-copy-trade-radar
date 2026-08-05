#!/usr/bin/env python3
"""
Prediction-Market Intel Ranker — v4
WorldMonitor-style + mean-reversion/regime-aware enhancements.
Telegram alerting + chart export included.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import List, Optional

import feedparser
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import requests

# ── Config ──────────────────────────────────────────────────────────────
RSS_FEEDS = [
    "http://feeds.reuters.com/reuters/topNews",
    "http://feeds.reuters.com/Reuters/worldNews",
    "http://feeds.bbci.co.uk/news/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
]

GDELT_QUERIES = [
    "prediction market",
    "Bitcoin OR Ethereum",
    "US election 2028",
    "Federal Reserve rates",
    "Ukraine ceasefire",
]
GDELT_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"

SIGNAL_WEIGHTS = {
    "geopolitics": 1.5,
    "election": 2.5,
    "central_bank": 1.8,
    "commodity": 1.2,
    "crypto": 2.0,
    "tech": 1.0,
    "disaster": 1.3,
    "trade": 1.4,
    "general": 0.2,
}

WINDOW_HOURS = 24
MAX_HEADLINES_PER_SOURCE = 60
MIN_LIQUIDITY = 5_000.0
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8765045454")


POS = {
    "yes", "win", "gain", "approve", "pass", "rise", "beat", "surge",
    "success", "deal", "agreement", "cut", "low", "falls", "decline",
    "drops", "weak", "bearish", "recession", "crisis", "collapse",
    "halt", "pause", "slows", "miss", "delay", "postpone", "cancels",
    "emergency", "outbreak", "landfall", "disruption", "broken",
    "resign", "oust", "defeat", "retreat", "sanction", "embargo",
}
NEG = {
    "no", "lose", "loss", "fail", "reject", "deny", "blocked",
    "surge", "soar", "rally", "jump", "bullish", "strong", "growth",
    "hike", "raise", "inflation", "tight", "hard", "rebound",
    "recover", "stabilize", "booms", "expands", "upgrade",
}


# ── Data shapes ──────────────────────────────────────────────────────────

@dataclass
class Headline:
    source: str
    title: str
    link: Optional[str] = None
    published: Optional[dt.datetime] = None
    tags: List[str] = field(default_factory=list)
    sentiment: float = 0.0


@dataclass
class Signal:
    category: str
    velocity: int = 0
    momentum: float = 0.0
    avg_sentiment: float = 0.0
    regime_volatility: float = 0.0


@dataclass
class RankedMarket:
    market_id: str
    name: str
    category: str
    score: float
    matched_signals: List[str]
    top_headline: Optional[str] = None
    outcome_bias: str = "neutral"
    current_price: Optional[float] = None
    mean_reversion_edge: float = 0.0
    liquidity: float = 0.0
    volume_24h: float = 0.0
    divergence: float = 0.0
    movement_score: float = 0.0
    engagement_score: float = 0.0
    price_movement: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────

def _parse_published(entry) -> Optional[dt.datetime]:
    for attr in ("published_parsed", "updated_parsed"):
        tp = getattr(entry, attr, None)
        if tp:
            try:
                return dt.datetime(*tp[:6], tzinfo=dt.timezone.utc)
            except Exception:
                pass
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip(" -\u00a0")


def _age_hours(pub: Optional[dt.datetime]) -> float:
    if pub is None:
        return 999.0
    now = dt.datetime.now(dt.timezone.utc)
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=dt.timezone.utc)
    delta = now - pub
    return max(delta.total_seconds() / 3600.0, 0.0)


def _parse_outcome_prices(raw: Optional[str]) -> Optional[List[float]]:
    if not raw:
        return None
    try:
        vals = json.loads(raw)
        if isinstance(vals, list) and len(vals) >= 2:
            return [float(vals[0]), float(vals[1])]
    except Exception:
        pass
    return None


def headline_sentiment(text: str) -> float:
    lower = text.lower()
    tokens = re.findall(r"[a-z']+", lower)
    pos = sum(1 for t in tokens if t in POS)
    neg = sum(1 for t in tokens if t in NEG)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


# ── Ingest layer ─────────────────────────────────────────────────────────

def fetch_gdelt_headlines() -> List[Headline]:
    out: List[Headline] = []
    for query in GDELT_QUERIES:
        url = f"{GDELT_BASE}?query={requests.utils.quote(query)}&format=rss&maxrows=40&timeline=1d"
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                title = _clean(getattr(e, "title", ""))
                if not title:
                    continue
                pub = _parse_published(e)
                link = getattr(e, "link", None)
                sent = headline_sentiment(title)
                out.append(Headline(
                    source="gdelt", title=title, link=link, published=pub, sentiment=sent
                ))
        except Exception as exc:
            print(f"[warn] gdelt feed failed: {query} :: {exc}", file=sys.stderr)
    return out


def fetch_headlines() -> List[Headline]:
    headlines: List[Headline] = []
    seen = set()
    cut = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=WINDOW_HOURS)

    sources = [("rss", url) for url in RSS_FEEDS] + [("gdelt", None)]
    for kind, url in sources:
        if kind == "rss":
            src = url.split("//")[-1].split("/")[0]
            try:
                feed = feedparser.parse(url)
                n = 0
                for e in feed.entries:
                    if n >= MAX_HEADLINES_PER_SOURCE:
                        break
                    title = _clean(getattr(e, "title", ""))
                    if not title:
                        continue
                    pub = _parse_published(e)
                    if pub and pub < cut:
                        continue
                    link = getattr(e, "link", None)
                    key = hashlib.md5((src + title).encode()).hexdigest()
                    if key in seen:
                        continue
                    seen.add(key)
                    sent = headline_sentiment(title)
                    headlines.append(Headline(
                        source=src, title=title, link=link, published=pub, sentiment=sent
                    ))
                    n += 1
            except Exception as exc:
                print(f"[warn] feed failed: {url} :: {exc}", file=sys.stderr)
        elif kind == "gdelt":
            for h in fetch_gdelt_headlines():
                key = hashlib.md5((h.source + h.title).encode()).hexdigest()
                if key in seen:
                    continue
                seen.add(key)
                headlines.append(h)

    headlines.sort(key=lambda h: h.published or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
    return headlines


# ── Signals ──────────────────────────────────────────────────────────────

CATEGORY_KEYWORDS: dict[str, List[str]] = {
    "geopolitics": ["war", "conflict", "invasion", "sanctions", "nuclear", "missile", "military", "alliance", "treaty", "summit",
                    "ukraine", "russia", "nato", "iran", "taiwan", "china", "peace talks"],
    "election":    ["election", "vote", "ballot", "presidential", "parliament", "poll", "incumbent", "candidate", "primary", "swing state",
                    "senate", "congress", "campaign", "2028"],
    "central_bank": ["fed", "ecb", "boj", "rate decision", "inflation", "cpi", "rate hike", "rate cut", "monetary policy", "yield curve",
                    "interest rate", "powell", "lagarde", "fomc"],
    "commodity":   ["oil", "brent", "wti", "gold", "copper", "lng", "wheat", "opec", "chokepoint", "commodity", "lumber"],
    "crypto":      ["bitcoin", "btc", "ethereum", "eth", "stablecoin", "crypto", "blockchain", "sec ", "etf", "solana", "xrp"],
    "tech":        ["ai ", "openai", "nvidia", "tsmc", "semiconductor", "chip", "google", "microsoft", "apple", "antitrust"],
    "disaster":    ["earthquake", "flood", "hurricane", "typhoon", "wildfire", "pandemic", "virus", "outbreak", "blackout", "storm"],
    "trade":       ["tariff", "trade deal", "trade war", "export", "import", "wto", "customs", "subsidy", "embargo"],
}

CATEGORY_ANTI_KEYWORDS: dict[str, List[str]] = {
    "general": ["gta", "grand theft", "playboi carti", "rihanna album", "jesus christ return",
                "soccer", "football", "world cup", "album", "celebrity", "pop culture"],
}


def tag_headline(h: Headline) -> List[str]:
    text = (h.title + " " + (h.source or "")).lower()
    out: List[str] = []
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                out.append(cat)
                break
    return out or ["general"]


def extract_signals(headlines: List[Headline]) -> List[Signal]:
    now = dt.datetime.now(dt.timezone.utc)
    buckets: dict[str, List[float]] = defaultdict(list)
    sent_buckets: dict[str, List[float]] = defaultdict(list)

    for h in headlines:
        cats = tag_headline(h)
        age_h = _age_hours(h.published)
        decay = max(0.0, 1.0 - age_h / WINDOW_HOURS)
        for c in cats:
            buckets[c].append(decay)
            sent_buckets[c].append(h.sentiment * decay)

    signals: List[Signal] = []
    for cat in sorted(set(CATEGORY_KEYWORDS) | set(CATEGORY_ANTI_KEYWORDS) | {"general"}):
        dec = buckets.get(cat, [])
        if not dec:
            continue
        vol = float(np.var(dec)) if len(dec) > 1 else 0.0
        avg_sent = float(np.mean(sent_buckets.get(cat, [0.0])))
        signals.append(Signal(category=cat, velocity=len(dec),
                               momentum=float(np.sum(dec)),
                               avg_sentiment=avg_sent,
                               regime_volatility=vol))
    return signals


def best_headline_for_market(market: dict, headlines: List[Headline]) -> Optional[str]:
    kws = [k.lower() for k in (market.get("keywords") or [])]
    if not kws:
        name = market.get("name", "").lower()
        kws = [w for w in name.replace("?", "").split() if len(w) > 3]
    for h in headlines[: min(200, len(headlines))]:
        text = h.title.lower()
        if any(k in text for k in kws):
            return h.title
    return None


# ── Live Polymarket pull ─────────────────────────────────────────────────

def fetch_polymarket_markets(limit: int = 120) -> List[dict]:
    markets: List[dict] = []
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"closed": "false", "limit": str(limit)},
            timeout=20,
        )
        if r.ok:
            raw = r.json()
            markets = raw if isinstance(raw, list) else (raw.get("data") or raw.get("markets") or [])
    except Exception as exc:
        print(f"[warn] Polymarket gamma api failed: {exc}", file=sys.stderr)
    return markets


def _normalize_pm_market(m: dict) -> dict:
    prices = _parse_outcome_prices(m.get("outcomePrices"))
    return {
        "market_id": str(m.get("id") or m.get("condition_id") or m.get("slug") or m.get("question", "")),
        "name": m.get("question") or m.get("name") or m.get("title") or "",
        "category": "",
        "current_price": prices[0] if prices else None,
        "liquidity": float(m.get("liquidityNum") or m.get("liquidity") or 0.0),
        "volume_24h": float(m.get("volume24hrClob") or m.get("volume24hr") or 0.0),
        # ── Real-money movement + engagement fields (stolen from last30days polymarket.py) ──
        "volume_num": float(m.get("volumeNum") or m.get("volume") or 0.0),
        "competitive": float(m.get("competitive") or 0.0),
        "one_day_change": float(m.get("oneDayPriceChange") or 0.0),
        "one_week_change": float(m.get("oneWeekPriceChange") or 0.0),
        "one_month_change": float(m.get("oneMonthPriceChange") or 0.0),
        "end_date": m.get("endDate") or m.get("endDateIso") or "",
    }


# ── Static catalog ───────────────────────────────────────────────────────

MARKET_CATALOG = [
    {"market_id": "pm-1",  "name": "Ukraine ceasefire by end of year?",              "category": "geopolitics",
     "keywords": ["ukraine", "ceasefire", "peace", "war", "russia", "negotiation", "talks"]},
    {"market_id": "pm-2",  "name": "Fed cuts rates this month?",                      "category": "central_bank",
     "keywords": ["fed", "rate", "cut", "hike", "decision", "powell", "fomc"]},
    {"market_id": "pm-3",  "name": "Bitcoin above $100K in 2026?",                   "category": "crypto",
     "keywords": ["bitcoin", "btc", "100k", "etf", "crypto"]},
    {"market_id": "pm-4",  "name": "OPEC+ cuts production next quarter?",            "category": "commodity",
     "keywords": ["opec", "oil", "production", "cuts", "barrel", "crude"]},
    {"market_id": "pm-5",  "name": "US AI regulation bill passes in 2026?",           "category": "tech",
     "keywords": ["ai ", "artificial intelligence", "regulation", "bill", "congress", "senate"]},
    {"market_id": "pm-6",  "name": "Major hurricane makes US landfall this season?",  "category": "disaster",
     "keywords": ["hurricane", "storm", "landfall", "atlantic", "category"]},
    {"market_id": "pm-7",  "name": "US-China tariff hike this month?",                "category": "trade",
     "keywords": ["tariff", "china", "trade war", "trade"]},
    {"market_id": "pm-8",  "name": "WHO declares new pandemic emergency?",            "category": "disaster",
     "keywords": ["pandemic", "who", "emergency", "virus", "outbreak"]},
    {"market_id": "pm-9",  "name": "Suez Canal disruption event this year?",         "category": "trade",
     "keywords": ["suez", "canal", "shipping", "vessel", "disruption"]},
    {"market_id": "pm-10", "name": "NATO expands membership this year?",             "category": "geopolitics",
     "keywords": ["nato", "expansion", "membership", "alliance"]},
    {"market_id": "pm-11", "name": "Gold above $2,500 by Q4?",                        "category": "commodity",
     "keywords": ["gold", "2500", "precious", "metal"]},
    {"market_id": "pm-12", "name": "US presidential election 2028: Democrat wins?",   "category": "election",
     "keywords": ["election", "presidential", "democrat", "republican", "vote", "2028"]},
    {"market_id": "pm-13", "name": "Ethereum flips Bitcoin market cap?",              "category": "crypto",
     "keywords": ["ethereum", "flip", "market cap", "bitcoin", "eth"]},
    {"market_id": "pm-14", "name": "Strait of Hormuz disruption event?",              "category": "geopolitics",
     "keywords": ["strait of hormuz", "hormuz", "chokepoint", "oil", "iran"]},
    {"market_id": "pm-15", "name": "ECB cuts rates in Q3?",                           "category": "central_bank",
     "keywords": ["ecb", "rate", "cut", "euro", "lagarde"]},
]


# ── Ranking ──────────────────────────────────────────────────────────────

def _is_trivial_market(name: str) -> bool:
    n = name.lower()
    trivial = [
        "gta ", "grand theft", "playboi carti", "rihanna album",
        "jesus christ return", "soccer", "football", "world cup",
        "album", "celebrity", "kim kardashian", "lebron james",
        "oprah winfrey", "george clooney", "mrbeast",
        "barack obama", "hillary clinton", "harvey weinstein",
        "2028 democratic presidential nomination", "presidential nomination",
        "playstation", "nintendo", "netflix show", "movie",
        "box office", "oscar", "grammy",
    ]
    return any(k in n for k in trivial)


def _market_category_name(m: dict) -> str:
    name = (m.get("name") or "").lower()
    if _is_trivial_market(name):
        return "general"
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(k in name for k in keywords):
            return cat
    return "general"


def _mean_reversion_edge(price: Optional[float]) -> float:
    if price is None:
        return 0.0
    distance = abs(price - 0.5)
    return round(distance * distance * 4.0, 4)


def _bias_from_sentiment(sent: float) -> str:
    if sent > 0.15:
        return "yes-leaning"
    if sent < -0.15:
        return "no-leaning"
    return "neutral"


def _safe_float(val, default: float = 0.0) -> float:
    """Safely convert a value to float (Gamma fields can be missing/None)."""
    try:
        return float(val if val is not None else default)
    except (ValueError, TypeError):
        return default


def _format_price_movement(market: dict) -> Optional[str]:
    """Pick the most significant price change and format it.

    Mirrors last30days polymarket.py: e.g. 'down 11.7% this week', or None
    if no change exceeds the 1% noise floor.
    """
    changes = [
        (abs(_safe_float(market.get("one_day_change"))), market.get("one_day_change"), "today"),
        (abs(_safe_float(market.get("one_week_change"))), market.get("one_week_change"), "this week"),
        (abs(_safe_float(market.get("one_month_change"))), market.get("one_month_change"), "this month"),
    ]
    changes.sort(key=lambda x: x[0], reverse=True)
    abs_change, raw_change, period = changes[0]
    if abs_change < 0.01:
        return None
    direction = "up" if _safe_float(raw_change) > 0 else "down"
    return f"{direction} {abs_change * 100:.1f}% {period}"


def _movement_score(market: dict) -> float:
    """Real-money movement signal (0..1). Day weighted 3x, week 2x, month 1x.

    20% absolute change in any window => 1.0. Stolen from last30days.
    """
    day = abs(_safe_float(market.get("one_day_change")))
    week = abs(_safe_float(market.get("one_week_change")))
    month = abs(_safe_float(market.get("one_month_change")))
    max_change = max(day * 3, week * 2, month)
    return min(1.0, max_change * 5.0)


def _engagement_score(market: dict) -> float:
    """Engagement = real-money interest (0..1): 50% volume, 25% liquidity,
    15% movement, 10% competitiveness (near 50/50 more interesting).

    Log-scaled so ~$9M volume ~= 1.0 and ~$1.2M liquidity ~= 1.0, matching
    the calibration in last30days polymarket.py.
    """
    vol_raw = _safe_float(market.get("volume_num")) or _safe_float(market.get("volume_24h"))
    liq = _safe_float(market.get("liquidity"))
    vol_score = min(1.0, math.log1p(vol_raw) / 16.0)
    liq_score = min(1.0, math.log1p(liq) / 14.0)
    movement = _movement_score(market)
    competitive = _safe_float(market.get("competitive"))
    return min(1.0, 0.50 * vol_score + 0.25 * liq_score + 0.15 * movement + 0.10 * competitive)


def score_market(market: dict, sig_map: dict[str, Signal]) -> RankedMarket:
    cat = _market_category_name(market)
    sig = sig_map.get(cat, sig_map.get("general", Signal(category=cat)))
    weight = SIGNAL_WEIGHTS.get(cat, 1.0)

    base = sig.momentum * weight
    reversion = _mean_reversion_edge(market.get("current_price"))
    liquidity_boost = math.log1p(market.get("liquidity", 0.0)) / 10.0

    # ── Real-money layer (stolen from last30days polymarket.py) ──
    movement = _movement_score(market)
    engagement = _engagement_score(market)
    # Boost scaled by category weight so it refines, not flattens, the news layer.
    real_money_boost = engagement * weight * 0.6

    vol_penalty = 1.0
    if sig.regime_volatility > 0.15:
        vol_penalty = 0.7

    score = round(
        (base * vol_penalty + reversion * weight * 0.8)
        + liquidity_boost
        + real_money_boost,
        4,
    )
    bias = _bias_from_sentiment(sig.avg_sentiment)
    signals = [cat] if sig.velocity > 0 else []

    # Divergence: high news velocity but price still near 0.5 (undecided)
    # -> news has NOT yet been priced in. Only meaningful if category has signal.
    divergence = 0.0
    if sig.velocity > 0 and market.get("current_price") is not None:
        price = market["current_price"]
        undecided = max(0.0, 1.0 - 2.0 * abs(price - 0.5))
        divergence = round(sig.momentum * undecided, 4)

    return RankedMarket(
        market_id=market["market_id"],
        name=market["name"],
        category=cat,
        score=score,
        matched_signals=signals,
        top_headline=market.get("top_headline"),
        outcome_bias=bias,
        current_price=market.get("current_price"),
        mean_reversion_edge=reversion,
        liquidity=market.get("liquidity", 0.0),
        volume_24h=market.get("volume_24h", 0.0),
        divergence=divergence,
        movement_score=round(movement, 4),
        engagement_score=round(engagement, 4),
        price_movement=_format_price_movement(market),
    )


# ── Output helpers ───────────────────────────────────────────────────────

def print_ranked(ranked: List[RankedMarket], signals: List[Signal]):
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n=== Prediction-Market Intel Ranker — {now} ===")

    print("\n[Signal Strength]")
    for s in sorted(signals, key=lambda x: x.momentum, reverse=True)[:12]:
        print(f"  {s.category:<14} velocity={s.velocity:<4} momentum={s.momentum:.3f} sentiment={s.avg_sentiment:+.3f}")

    print("\n[Top Market Moves]")
    shown = 0
    for mo in ranked:
        if mo.score <= 0:
            continue
        bias = mo.outcome_bias.replace("yes-leaning", "⬇ YES lean").replace("no-leaning", "⬆ NO lean").replace("neutral", "neutral")
        price_str = f" price={mo.current_price:.3f}" if mo.current_price is not None else ""
        rev_str = f" reversion={mo.mean_reversion_edge:.3f}" if mo.mean_reversion_edge else ""
        mov_str = f" | {mo.price_movement}" if mo.price_movement else ""
        print(f"  {shown+1:>2}. [{mo.score:0.3f}] {mo.name}{price_str}{rev_str}{mov_str}")
        print(f"      category={mo.category} | {bias} | liq={mo.liquidity:,.0f} | vol24h={mo.volume_24h:,.0f}")
        if mo.top_headline:
            print(f"      ↳ {mo.top_headline}")
        shown += 1
        if shown >= 20:
            break

    print("\n[News-Price Divergence Alerts]")
    divs = sorted(
        [r for r in ranked if r.divergence > 0.5],
        key=lambda x: x.divergence, reverse=True
    )[:5]
    if not divs:
        print("  (none significant right now)")
    for r in divs:
        price = f"{r.current_price:.3f}" if r.current_price is not None else "n/a"
        print(f"  • {r.name}")
        print(f"    div={r.divergence:.2f} | price={price} | {r.category}")
        if r.top_headline:
            print(f"    ↳ {r.top_headline}")

    print("\n[Price Movers — real-money movement (stolen from last30days)]")
    movers = sorted(
        [r for r in ranked if r.movement_score > 0.05],
        key=lambda x: x.movement_score, reverse=True
    )[:8]
    if not movers:
        print("  (no significant price movement right now)")
    for r in movers:
        mv = r.price_movement or "n/a"
        print(f"  • {r.name}")
        print(f"    move={r.movement_score:.2f} | {mv} | liq={r.liquidity:,.0f} | eng={r.engagement_score:.2f}")

    print("\nDisclaimer: lightweight research aid, not financial advice. Verify on Polymarket.\n")


def save_chart(ranked: List[RankedMarket], signals: List[Signal], path: str) -> str:
    top = [r for r in ranked if r.score > 0][:8]
    if not top:
        return ""
    names = [r.name[:38] + ("…" if len(r.name) > 38 else "") for r in top]
    scores = [r.score for r in top]
    cats = [r.category for r in top]

    cat_colors = {
        "geopolitics": "#D4AF37",
        "election": "#7B3FBF",
        "central_bank": "#1F6F9B",
        "crypto": "#F7931A",
        "commodity": "#2E5339",
        "tech": "#0F172A",
        "disaster": "#C0392B",
        "trade": "#E67E22",
    }
    bar_colors = [cat_colors.get(c, "#555555") for c in cats]

    fig, ax = plt.subplots(figsize=(12, 6), dpi=150)
    ax.barh(names[::-1], scores[::-1], color=bar_colors[::-1])
    ax.set_xlabel("Intel Score", fontsize=11)
    ax.set_title("Polymarket Intel Ranker — Top Markets", fontsize=14, weight="bold", color="#1A1A1A")
    ax.grid(axis="x", alpha=0.2)
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
    plt.tight_layout()
    plt.savefig(path, transparent=False)
    plt.close()
    return path


def build_telegram_text(ranked: List[RankedMarket], signals: List[Signal]) -> str:
    top = [r for r in ranked if r.score > 0][:3]
    if not top:
        return "No high-signal markets right now."
    lines = ["📊 *Top 3 Prediction-Market Signals*\n"]
    for i, r in enumerate(top, 1):
        bias = r.outcome_bias.replace("yes-leaning", "YES lean").replace("no-leaning", "NO lean").replace("neutral", "neutral")
        price = f"{r.current_price:.3f}" if r.current_price is not None else "n/a"
        lines.append(f"{i}. *{r.name}*")
        lines.append(f"   Score: {r.score:.3f} | Price: {price} | {bias}")
        lines.append(f"   Category: {r.category}")
        if r.price_movement:
            lines.append(f"   📈 Move: {r.price_movement} (engagement {r.engagement_score:.2f})")
        if r.top_headline:
            lines.append(f"   ↳ {r.top_headline}")
        lines.append("")
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────

def run_pipeline() -> dict:
    """Run the full ranking pipeline and return a payload dict.

    Returns:
        {
            "generated_at": ISO timestamp,
            "signals": [Signal as dict],
            "markets": [RankedMarket as dict],
            "live_polymarket_count": int,
            "chart": path to PNG or "",
            "top3_text": telegram-formatted string,
        }
    """
    headlines = fetch_headlines()
    signals = extract_signals(headlines)
    sig_map = {s.category: s for s in signals}

    live = fetch_polymarket_markets(limit=120)

    normed: List[dict] = []
    for m in live:
        nm = _normalize_pm_market(m)
        if not nm["name"]:
            continue
        if nm.get("liquidity", 0.0) < MIN_LIQUIDITY:
            continue
        nm["category"] = _market_category_name(nm)
        nm["top_headline"] = best_headline_for_market(nm, headlines)
        normed.append(nm)

    live_ids = {m["market_id"] for m in normed}
    combined: List[dict] = list(normed)
    for m in MARKET_CATALOG:
        if m["market_id"] not in live_ids:
            m["top_headline"] = best_headline_for_market(m, headlines)
            m.setdefault("liquidity", 0.0)
            m.setdefault("volume_24h", 0.0)
            m.setdefault("current_price", None)
            m.setdefault("end_date", "")
            combined.append(m)

    filtered: List[dict] = [m for m in combined if not _is_trivial_market(m.get("name", ""))]
    ranked = sorted((score_market(m, sig_map) for m in filtered), key=lambda x: x.score, reverse=True)
    ranked = [r for r in ranked if r.score > 0][:25]

    def _sort_key(r: RankedMarket):
        has_signal = 1 if r.matched_signals and r.matched_signals != ["general"] else 0
        return (has_signal, r.score)
    ranked.sort(key=_sort_key, reverse=True)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    chart_path = os.path.join(out_dir, "pm_intel_rank_chart.png")
    chart = save_chart(ranked, signals, chart_path)

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "signals": [asdict(s) for s in signals],
        "markets": [asdict(m) for m in ranked],
        "live_polymarket_count": len(live),
        "chart": chart,
        "top3_text": build_telegram_text(ranked, signals),
    }


def main():
    print("[*] Fetching headlines ...")
    headlines = fetch_headlines()
    print(f"    got {len(headlines)} headlines in last {WINDOW_HOURS}h")

    print("[*] Extracting signals ...")
    signals = extract_signals(headlines)
    sig_map = {s.category: s for s in signals}

    print("[*] Fetching live Polymarket markets ...")
    live = fetch_polymarket_markets(limit=120)
    print(f"    live markets fetched: {len(live)}")

    normed: List[dict] = []
    for m in live:
        nm = _normalize_pm_market(m)
        if not nm["name"]:
            continue
        if nm.get("liquidity", 0.0) < MIN_LIQUIDITY:
            continue
        nm["category"] = _market_category_name(nm)
        nm["top_headline"] = best_headline_for_market(nm, headlines)
        normed.append(nm)

    live_ids = {m["market_id"] for m in normed}
    combined: List[dict] = list(normed)
    for m in MARKET_CATALOG:
        if m["market_id"] not in live_ids:
            m["top_headline"] = best_headline_for_market(m, headlines)
            m.setdefault("liquidity", 0.0)
            m.setdefault("volume_24h", 0.0)
            m.setdefault("current_price", None)
            m.setdefault("end_date", "")
            combined.append(m)

    filtered: List[dict] = [m for m in combined if not _is_trivial_market(m.get("name", ""))]
    ranked = sorted((score_market(m, sig_map) for m in filtered), key=lambda x: x.score, reverse=True)
    ranked = [r for r in ranked if r.score > 0][:25]

    def _sort_key(r: RankedMarket):
        has_signal = 1 if r.matched_signals and r.matched_signals != ["general"] else 0
        return (has_signal, r.score)
    ranked.sort(key=_sort_key, reverse=True)

    print_ranked(ranked, signals)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "pm_intel_rank.json")
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "signals": [asdict(s) for s in signals],
        "markets": [asdict(m) for m in ranked],
        "live_polymarket_count": len(live),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[*] Saved JSON output: {out_path}")

    chart_path = os.path.join(out_dir, "pm_intel_rank_chart.png")
    save_chart(ranked, signals, chart_path)
    print(f"[*] Saved chart: {chart_path}")

    alert_path = os.path.join(out_dir, "pm_intel_alert_top3.json")
    with open(alert_path, "w", encoding="utf-8") as f:
        json.dump({
            "text": build_telegram_text(ranked, signals),
            "chart": chart_path,
            "top3": [asdict(r) for r in ranked[:3]],
        }, f, ensure_ascii=False, indent=2)
    print(f"[*] Saved alert payload: {alert_path}")


if __name__ == "__main__":
    main()
