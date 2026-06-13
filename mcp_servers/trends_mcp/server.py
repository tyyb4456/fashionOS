"""
trends-mcp — FashionOS MCP Server
Exposes Google Trends data (via Pytrends) as MCP tools.

Read tools:
  get_trend_data      → Interest over time for 1–5 keywords
  get_related_queries → Rising + top related searches for a keyword
  compare_keywords    → Relative interest comparison with direction signal

Architecture:
  Pytrends is a synchronous library (uses requests internally).
  FastMCP tools are async. Each tool runs the synchronous pytrends call
  in a ThreadPoolExecutor so the event loop is never blocked.

  A fresh TrendReq is created per call — pytrends is not thread-safe and
  reusing instances across concurrent requests causes data corruption.

Rate limiting:
  Google Trends aggressively rate-limits automated requests.
  TrendReq is configured with retries=2 + backoff_factor=0.5.
  On TooManyRequestsError, the tool returns {"error": "rate_limited", ...}.
  The Trend Agent handles empty/error responses gracefully (empty signals).

Geo defaults:
  TRENDS_DEFAULT_GEO=PK  → Pakistan-specific data by default.
  Pass geo="" for worldwide data.

Port: 8003
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

# ── urllib3 v2 compat patch ───────────────────────────────────────────────────
import urllib3.util.retry as _urllib3_retry
_orig_retry_init = _urllib3_retry.Retry.__init__
def _patched_retry_init(self, *args, method_whitelist=None, **kwargs):
    if method_whitelist is not None:
        kwargs.setdefault("allowed_methods", method_whitelist)
    _orig_retry_init(self, *args, **kwargs)
_urllib3_retry.Retry.__init__ = _patched_retry_init

# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_GEO = os.getenv("TRENDS_DEFAULT_GEO", "PK")

# Thread pool for synchronous pytrends calls — 2 workers is enough
# (Google rate-limits anyway; more workers just means more 429s faster)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pytrends")


# ── FastMCP app ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="trends-mcp",
    instructions=(
        "Access Google Trends data for fashion keyword analysis. "
        "All values are relative interest (0–100 scale), NOT absolute search volumes. "
        "100 = peak popularity for that keyword in the given period. "
        "Default geo is PK (Pakistan). Pass geo='' for worldwide. "
        "Used by Trend Agent to confirm or cross-reference TikTok/Instagram signals."
    ),
)


# ── Sync helpers (run in ThreadPoolExecutor) ──────────────────────────────────

def _build_pytrends():
    """
    Build a fresh TrendReq. Never reuse across concurrent calls.

    timeout=(connect_s, read_s) — Google Trends can be slow.
    retries + backoff_factor built into the requests session.
    """
    from pytrends.request import TrendReq
    return TrendReq(
        hl="en-US",
        tz=300,                    # PKT = UTC+5
        timeout=(10, 30),
        retries=2,
        backoff_factor=0.5,
        requests_args={"verify": True},
    )


def _sync_get_trend_data(
    keywords:  list[str],
    timeframe: str,
    geo:       str,
) -> list[dict]:
    """Interest over time per keyword. Returns [] on empty or rate-limit."""
    pytrends = _build_pytrends()
    pytrends.build_payload(keywords[:5], timeframe=timeframe, geo=geo)
    df = pytrends.interest_over_time()

    if df is None or df.empty:
        return []

    if "isPartial" in df.columns:
        df = df.drop(columns=["isPartial"])

    results = []
    for date, row in df.iterrows():
        entry = {"date": str(date.date())}
        for kw in keywords[:5]:
            if kw in row.index:
                entry[kw] = int(row[kw])
        results.append(entry)
    return results


def _sync_get_related_queries(keyword: str, geo: str) -> dict:
    """
    Rising queries have the highest signal — they show emerging searches
    before they hit mainstream.

    A "breakout" value (2000+) means extremely rapid growth — treat these
    as high-confidence emerging trend signals.
    """
    pytrends = _build_pytrends()
    pytrends.build_payload([keyword], geo=geo)
    related = pytrends.related_queries()

    result: dict = {"rising": [], "top": []}
    if not related or keyword not in related:
        return result

    kw_data = related[keyword]

    rising_df = kw_data.get("rising")
    if rising_df is not None and not rising_df.empty:
        result["rising"] = [
            {
                "query":    str(row["query"]),
                "value":    int(row["value"]),
                "breakout": int(row["value"]) >= 2000,
            }
            for _, row in rising_df.head(10).iterrows()
        ]

    top_df = kw_data.get("top")
    if top_df is not None and not top_df.empty:
        result["top"] = [
            {"query": str(row["query"]), "value": int(row["value"])}
            for _, row in top_df.head(10).iterrows()
        ]

    return result


def _sync_compare_keywords(
    keywords:  list[str],
    timeframe: str,
    geo:       str,
) -> list[dict]:
    """
    Compares keywords and returns avg/peak/latest interest + direction.

    Direction = "rising" if the last data point is higher than 4 points ago
    (roughly 1 week back for daily data, 1 month back for weekly data).
    """
    pytrends = _build_pytrends()
    pytrends.build_payload(keywords[:5], timeframe=timeframe, geo=geo)
    df = pytrends.interest_over_time()

    if df is None or df.empty:
        return []

    if "isPartial" in df.columns:
        df = df.drop(columns=["isPartial"])

    results = []
    for kw in keywords[:5]:
        if kw not in df.columns:
            continue

        series   = df[kw]
        avg      = float(series.mean())
        peak     = int(series.max())
        latest   = int(series.iloc[-1]) if len(series) > 0 else 0
        lookback = int(series.iloc[-4]) if len(series) >= 4 else int(series.iloc[0])

        if latest > lookback + 5:
            direction = "rising"
        elif latest < lookback - 5:
            direction = "declining"
        else:
            direction = "stable"

        results.append({
            "keyword":          kw,
            "avg_interest":     round(avg, 1),
            "peak_interest":    peak,
            "latest_interest":  latest,
            "direction":        direction,
        })

    return sorted(results, key=lambda x: -x["avg_interest"])


# ── TOOLS ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_trend_data(
    keywords:  list[str],
    timeframe: str = "today 1-m",
    geo:       str = DEFAULT_GEO,
) -> list[dict]:
    """
    Get Google Trends interest over time for up to 5 keywords.

    Args:
        keywords:  Fashion keywords to track.
                   E.g. ["lawn suit", "co-ord set", "cargo pants"]
                   Max 5 keywords per call (Google Trends API limit).
        timeframe: Pytrends timeframe string.
                   "now 7-d"   → last 7 days (hourly granularity)
                   "today 1-m" → last 30 days (daily) ← default
                   "today 3-m" → last 90 days (weekly)
        geo:       Country code. "PK" = Pakistan (default). "" = worldwide.

    Returns time series — one dict per date:
        [{"date": "2025-06-01", "lawn suit": 85, "co-ord set": 42}, ...]

    All scores are 0–100 relative to the peak value in the period.
    100 = peak for that keyword in that period (NOT absolute search volume).

    On error returns: [{"error": "...", "keywords": [...]}]

    Used by: Trend Agent — confirm TikTok signals with search demand trend.
    """
    if not keywords:
        return []

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            _executor,
            _sync_get_trend_data,
            keywords[:5],
            timeframe,
            geo,
        )
    except Exception as exc:
        return [{"error": str(exc), "keywords": keywords}]


@mcp.tool()
async def get_related_queries(
    keyword: str,
    geo:     str = DEFAULT_GEO,
) -> dict:
    """
    Get rising and top related queries for a fashion keyword.

    Rising queries are the most valuable signal — they show terms that are
    growing much faster than their baseline. A breakout (value=2000+) means
    the query is rising so fast Google can't show a % (usually a new trend).

    Args:
        keyword: Fashion keyword to expand. E.g. "cargo pants", "co-ord set"
        geo:     Country code. "PK" = Pakistan (default). "" = worldwide.

    Returns:
        {
            "rising": [
                {"query": "olive cargo pants women", "value": 2000, "breakout": true},
                ...
            ],
            "top": [
                {"query": "cargo pants pakistan", "value": 100},
                ...
            ]
        }

    On error returns: {"error": "...", "keyword": keyword}

    Used by: Trend Agent — find emerging sub-trends for catalog expansion flags.
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            _executor,
            _sync_get_related_queries,
            keyword,
            geo,
        )
    except Exception as exc:
        return {"error": str(exc), "keyword": keyword}


@mcp.tool()
async def compare_keywords(
    keywords:  list[str],
    timeframe: str = "today 1-m",
    geo:       str = DEFAULT_GEO,
) -> list[dict]:
    """
    Compare relative search interest across multiple fashion keywords.

    Returns average, peak, and latest interest scores plus a direction
    signal ("rising" | "stable" | "declining"). Sorted by avg_interest
    descending — strongest trend first.

    Use this to:
    - Rank which trends in the catalog have the strongest search demand
    - Detect trend reversals before inventory decisions
    - Decide which products to push in content and ads

    Args:
        keywords:  Keywords to compare (max 5, Google Trends API limit).
        timeframe: "now 7-d", "today 1-m" (default), "today 3-m"
        geo:       Country code. "PK" = Pakistan (default). "" = worldwide.

    Returns sorted by avg_interest descending:
        [
            {
                "keyword":         "cargo pants",
                "avg_interest":    72.4,
                "peak_interest":   95,
                "latest_interest": 80,
                "direction":       "rising"
            },
            ...
        ]

    On error returns: [{"error": "...", "keywords": [...]}]

    Used by: Trend Agent — final signal ranking before writing to state.
             Content Agent (future) — which trends to write content about.
    """
    if not keywords:
        return []

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            _executor,
            _sync_compare_keywords,
            keywords[:5],
            timeframe,
            geo,
        )
    except Exception as exc:
        return [{"error": str(exc), "keywords": keywords}]


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=8003)