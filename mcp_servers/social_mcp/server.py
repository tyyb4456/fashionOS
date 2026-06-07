"""
social-mcp — FashionOS MCP Server
Exposes TikTok and Instagram scraping (via Apify) as MCP tools.

Read tools:
  search_tiktok_hashtag     → TikTok posts by hashtag (views, likes, shares, captions)
  search_instagram_hashtag  → Instagram posts by hashtag (likes, comments, type)
  get_trending_tiktok_sounds → Trending audio — a leading fashion trend indicator

Architecture:
  Uses Apify's /run-sync-get-dataset-items endpoint which blocks until the
  actor finishes (up to APIFY_SCRAPE_TIMEOUT_SECONDS, default 90s) and returns
  dataset items in the response body. No polling, no dataset fetching loop.

Actor IDs:
  TikTok:   APIFY_TIKTOK_ACTOR_ID   (default: clockworks/free-tiktok-scraper)
  Instagram: APIFY_INSTAGRAM_ACTOR_ID (default: apify/instagram-hashtag-scraper)
  Override in .env if Apify deprecates or renames them.

Error handling:
  All tools return a list. On any error (timeout, rate limit, actor failure),
  the tool returns [{"error": "<reason>", "source": "<platform>"}] — never raises.
  The Trend Agent's Node 3 handles empty/error responses gracefully.

Port: 8002
"""

import os
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

APIFY_TOKEN    = os.getenv("APIFY_API_TOKEN", "")
APIFY_BASE_URL = "https://api.apify.com/v2"

# Configurable actor IDs — override in .env when Apify renames these
TIKTOK_ACTOR_ID    = os.getenv("APIFY_TIKTOK_ACTOR_ID",    "clockworks/free-tiktok-scraper")
INSTAGRAM_ACTOR_ID = os.getenv("APIFY_INSTAGRAM_ACTOR_ID", "apify/instagram-hashtag-scraper")

# How long to wait for Apify to finish a scrape (seconds)
# Raise this if you see timeout errors; lower if you'd rather skip than block the pipeline
SCRAPE_TIMEOUT = int(os.getenv("APIFY_SCRAPE_TIMEOUT_SECONDS", "90"))


# ── FastMCP app ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="social-mcp",
    instructions=(
        "Access TikTok and Instagram social data for fashion trend analysis. "
        "All tools call Apify actors synchronously — expect 30–90 second response times. "
        "Returns normalised engagement metrics. "
        "Used by the Trend Agent to identify rising fashion content in Pakistan."
    ),
)


# ── HTTP helper ───────────────────────────────────────────────────────────────

async def _run_actor_sync(
    actor_id:  str,
    run_input: dict,
    limit:     int = 20,
) -> list[dict]:
    """
    Calls Apify's run-sync-get-dataset-items endpoint.

    Starts the actor and waits up to SCRAPE_TIMEOUT seconds for it to finish,
    then returns the dataset items in one shot. Raises HTTPStatusError on
    non-2xx responses so callers can catch and return an error record.

    Args:
        actor_id:  e.g. "clockworks/free-tiktok-scraper"
        run_input: Actor-specific input dict
        limit:     maxItems param — how many dataset rows to return

    Returns raw list of dicts from the actor's default dataset.
    """
    if not APIFY_TOKEN:
        raise ValueError(
            "APIFY_API_TOKEN is not set. "
            "Add it to mcp_servers/social_mcp/.env to enable social scraping."
        )

    url = f"{APIFY_BASE_URL}/acts/{actor_id}/run-sync-get-dataset-items"
    params = {
        "token":    APIFY_TOKEN,
        "maxItems": limit,
        "clean":    "true",     # skip items with no useful data
    }

    # +15s buffer beyond actor timeout so httpx doesn't cut off before Apify does
    async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT + 15) as client:
        r = await client.post(url, json=run_input, params=params)
        r.raise_for_status()
        return r.json()


# ── TOOLS ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def search_tiktok_hashtag(
    hashtag: str,
    limit:   int = 20,
    geo:     str = "PK",
) -> list[dict]:
    """
    Scrape TikTok posts for a hashtag and return normalised engagement signals.

    Args:
        hashtag: Hashtag WITHOUT the # symbol. E.g. "PakistaniFashion"
        limit:   Max posts to return (default 20, capped at 50).
        geo:     Country code for geo-targeting (default "PK" for Pakistan).

    Returns list of posts. Each post has:
        platform, hashtag, post_id, text (caption, 300 chars), author,
        views, likes, comments, shares, created_at, hashtags[], music

    On error returns: [{"error": "...", "hashtag": hashtag, "source": "tiktok"}]

    Used by: Trend Agent (Node 1 — fetch_social_data)
    """
    limit = min(limit, 50)

    run_input = {
        "hashtags":       [hashtag],
        "resultsPerPage": limit,
        "maxCrawledItems": limit,
        "proxy": {
            "useApifyProxy":    True,
            "apifyProxyGroups": ["RESIDENTIAL"],
        },
    }

    try:
        raw_items = await _run_actor_sync(TIKTOK_ACTOR_ID, run_input, limit=limit)
    except Exception as exc:
        return [{"error": str(exc), "hashtag": hashtag, "source": "tiktok"}]

    results = []
    for item in raw_items[:limit]:
        stats = item.get("stats", {})
        results.append({
            "platform":   "tiktok",
            "hashtag":    hashtag,
            "post_id":    item.get("id", ""),
            "text":       (item.get("text") or "")[:300],
            "author":     item.get("authorMeta", {}).get("name", ""),
            "views":      stats.get("playCount", 0),
            "likes":      stats.get("diggCount", 0),
            "comments":   stats.get("commentCount", 0),
            "shares":     stats.get("shareCount", 0),
            "created_at": item.get("createTimeISO", ""),
            "hashtags":   [h.get("name", "") for h in item.get("hashtags", [])],
            "music":      item.get("musicMeta", {}).get("musicName", ""),
        })

    return results


@mcp.tool()
async def search_instagram_hashtag(
    hashtag: str,
    limit:   int = 20,
) -> list[dict]:
    """
    Scrape Instagram posts for a hashtag and return normalised engagement signals.

    Args:
        hashtag: Hashtag WITHOUT the # symbol. E.g. "PakistaniFashion"
        limit:   Max posts to return (default 20, capped at 50).

    Returns list of posts. Each post has:
        platform, hashtag, post_id, shortcode, caption (300 chars),
        likes, comments, post_type (image|video|sidecar), created_at, url

    Note: Instagram does not expose saves via scraping. Use likes + comments
    as a purchase intent proxy.

    On error returns: [{"error": "...", "hashtag": hashtag, "source": "instagram"}]

    Used by: Trend Agent (Node 1), Content Agent (future)
    """
    limit = min(limit, 50)

    run_input = {
        "hashtags":     [hashtag],
        "resultsLimit": limit,
        "proxy": {
            "useApifyProxy": True,
        },
    }

    try:
        raw_items = await _run_actor_sync(INSTAGRAM_ACTOR_ID, run_input, limit=limit)
    except Exception as exc:
        return [{"error": str(exc), "hashtag": hashtag, "source": "instagram"}]

    results = []
    for item in raw_items[:limit]:
        results.append({
            "platform":   "instagram",
            "hashtag":    hashtag,
            "post_id":    item.get("id", ""),
            "shortcode":  item.get("shortCode", ""),
            "caption":    (item.get("caption") or "")[:300],
            "likes":      item.get("likesCount", 0),
            "comments":   item.get("commentsCount", 0),
            "post_type":  item.get("type", "image"),   # image | video | sidecar
            "created_at": item.get("timestamp", ""),
            "url":        item.get("url", ""),
        })

    return results


@mcp.tool()
async def get_trending_tiktok_sounds(
    limit: int = 10,
) -> list[dict]:
    """
    Extract trending TikTok sounds from Pakistani fashion content.

    Trending sounds are a *leading* trend indicator: when a sound goes viral
    with outfit content, the associated style typically sees demand spikes
    2–3 weeks later on Instagram/search. Use this to get ahead of the curve.

    Args:
        limit: Max unique sounds to return (default 10).

    Returns list of sounds:
        name, author, is_original, sample_post_views

    On error returns: [{"error": "...", "source": "tiktok_sounds"}]

    Used by: Trend Agent (Node 1)
    """
    # Pull recent Pakistani fashion posts and extract the sounds from them
    run_input = {
        "hashtags":       ["PakistaniFashion", "GRWM", "OutfitOfTheDay"],
        "resultsPerPage": limit * 3,   # fetch more posts to find diverse sounds
        "maxCrawledItems": limit * 3,
        "proxy": {"useApifyProxy": True},
    }

    try:
        raw_items = await _run_actor_sync(TIKTOK_ACTOR_ID, run_input, limit=limit * 3)
    except Exception as exc:
        return [{"error": str(exc), "source": "tiktok_sounds"}]

    seen_sounds: set[str] = set()
    sounds: list[dict]   = []

    for item in raw_items:
        music      = item.get("musicMeta", {})
        music_name = (music.get("musicName") or "").strip()
        if not music_name or music_name in seen_sounds:
            continue

        seen_sounds.add(music_name)
        sounds.append({
            "name":              music_name,
            "author":            music.get("musicAuthor", ""),
            "is_original":       music.get("musicOriginal", False),
            "sample_post_views": item.get("stats", {}).get("playCount", 0),
        })

        if len(sounds) >= limit:
            break

    return sounds


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=8002)