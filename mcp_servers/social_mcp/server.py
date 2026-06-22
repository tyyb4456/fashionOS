"""
social-mcp — FashionOS MCP Server
Exposes TikTok/Instagram scraping (Apify) + Instagram DM management (Graph API).

Scraping tools (Apify):
  search_tiktok_hashtag      → TikTok posts by hashtag
  search_instagram_hashtag   → Instagram posts by hashtag
  get_trending_tiktok_sounds → Trending audio signals

DM tools (Instagram Graph API) — NEW session 7:
  get_instagram_dms          → Unread DM conversations from business account
  send_instagram_dm          → Send a reply DM to a customer
  get_instagram_comments     → Comments on a specific post

Two auth contexts:
  Scraping: APIFY_API_TOKEN
  DMs:      INSTAGRAM_ACCESS_TOKEN + INSTAGRAM_PAGE_ID (Graph API page token)

Port: 8002
"""

import os
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
import redis.asyncio as _aioredis

load_dotenv()

# ── Multi-tenant credential fetching ─────────────────────────────────────────
# MCP servers are shared across all brands.
# Each tool receives brand_id and fetches credentials from Redis.

import redis.asyncio as _aioredis

_REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
_redis = _aioredis.from_url(_REDIS_URL, decode_responses=True)  # shared pool, not reconnected per call


async def _get_brand_creds(brand_id: str) -> dict:
    """
    Fetch decrypted brand credentials from Redis.
    The main API writes these when a brand is created or credentials are updated.
    Raises ValueError if brand_id is not found in cache — caller returns an error response.
    """
    try:
        raw = await _redis.get(f"fashionos:creds:{brand_id}")
        if not raw:
            raise ValueError(
                f"No credentials found for brand_id='{brand_id}'. "
                "Ensure the brand exists and POST /api/v1/brands was called first."
            )
        import json as _json
        return _json.loads(raw)
    finally:
        await _redis.aclose()

# ── Apify config (scraping) ───────────────────────────────────────────────────

APIFY_TOKEN    = os.getenv("APIFY_API_TOKEN", "")
APIFY_BASE_URL = "https://api.apify.com/v2"
SCRAPE_TIMEOUT = 90  # was undefined -> NameError on every call before

TIKTOK_ACTOR_ID    = os.getenv("APIFY_TIKTOK_ACTOR_ID",    "clockworks/free-tiktok-scraper")
INSTAGRAM_ACTOR_ID = os.getenv("APIFY_INSTAGRAM_ACTOR_ID", "apify/instagram-hashtag-scraper")

CACHE_TTL_SECONDS = 6 * 60 * 60  # trend data is stale-tolerant, don't re-pay Apify every request

# ── Instagram Graph API config (DMs) ─────────────────────────────────────────

IG_GRAPH_VERSION       = os.getenv("INSTAGRAM_GRAPH_API_VERSION", "v21.0")
IG_BASE_URL            = f"https://graph.facebook.com/{IG_GRAPH_VERSION}"


# ── FastMCP app ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="social-mcp",
    instructions=(
        "Access TikTok/Instagram social data (Apify scraping) and Instagram DM management "
        "(Graph API). Scraping tools for trend signals; DM tools for customer communication. "
        "Scraping calls take 30-90 seconds. DM calls are fast (<2s)."
    ),
)

async def _cache_get(key: str) -> Optional[list]:
    raw = await _redis.get(key)
    return json.loads(raw) if raw else None


async def _cache_set(key: str, value: list):
    await _redis.set(key, json.dumps(value), ex=CACHE_TTL_SECONDS)

def _engagement_score(views: int, likes: int, comments: int, shares: int, created_at: str) -> float:
    """Weighted engagement per hour since posting — surfaces what's *currently* gaining traction."""
    try:
        posted = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        hours = max(1.0, (datetime.now(timezone.utc) - posted).total_seconds() / 3600)
    except Exception:
        hours = 24.0
    weighted = views * 1 + likes * 3 + comments * 5 + shares * 4
    return weighted / hours


# ── Apify HTTP helper ─────────────────────────────────────────────────────────

async def _run_actor_sync(actor_id: str, run_input: dict, limit: int = 20, retries: int = 2) -> list[dict]:
    if not APIFY_TOKEN:
        raise ValueError("APIFY_API_TOKEN not set in social-mcp .env")

    actor_id_url = actor_id.replace("/", "~")
    url = f"{APIFY_BASE_URL}/acts/{actor_id_url}/run-sync-get-dataset-items"
    params = {"token": APIFY_TOKEN, "maxItems": limit, "clean": "true"}

    last_exc = None
    async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT) as client:
        for attempt in range(retries + 1):
            try:
                r = await client.post(url, json=run_input, params=params)
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (429, 500, 502, 503):
                    raise  # bad input etc — don't retry, fail fast
                last_exc = exc
            except httpx.TimeoutException as exc:
                last_exc = exc
            await asyncio.sleep(2 ** attempt)
    raise last_exc


# ── Instagram Graph API helpers ───────────────────────────────────────────────

async def _ig_get(brand_id: str, path: str, params: dict | None = None) -> dict:
    """GET to Instagram Graph API."""
    try:
        creds = await _get_brand_creds(brand_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    
    token = creds["instagram_access_token"]

    all_params = {"access_token": token, **(params or {})}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{IG_BASE_URL}/{path}", params=all_params)
        r.raise_for_status()
        return r.json()


async def _ig_post(brand_id: str, path: str, payload: dict) -> dict:
    """POST to Instagram Graph API."""
    try:
        creds = await _get_brand_creds(brand_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    
    token = creds["instagram_access_token"]

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{IG_BASE_URL}/{path}",
            params={"access_token": token},
            json=payload,
        )
        r.raise_for_status()
        return r.json()


# ══════════════════════════════════════════════════════════════════════════════
# SCRAPING TOOLS (unchanged from session 6)
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def search_tiktok_hashtag(hashtag: str, limit: int = 20) -> list[dict]:
    """
    Scrape TikTok posts for a hashtag, ranked by engagement velocity (highest first).

    Args:
        hashtag: Hashtag WITHOUT the # symbol. E.g. "PakistaniFashion"
        limit:   Max posts to return (default 20, capped at 50).

    Note: clockworks/free-tiktok-scraper has no geo/country input field — there's
    no way to geo-filter this actor, so that param was removed rather than faked.
    """
    
    hashtag = hashtag.lstrip("#").strip().lower()
    limit = min(limit, 50)
    cache_key = f"fashionos:trend:tiktok:{hashtag}:{limit}"

    if (cached := await _cache_get(cache_key)) is not None:
        return cached

    run_input = {"hashtags": [hashtag], "resultsPerPage": limit}

    try:
        raw_items = await _run_actor_sync(TIKTOK_ACTOR_ID, run_input, limit=limit)
    except Exception as exc:
        return [{"error": str(exc), "hashtag": hashtag, "source": "tiktok"}]

    results = []
    for item in raw_items[:limit]:
        views, likes = item.get("playCount", 0), item.get("diggCount", 0)
        comments, shares = item.get("commentCount", 0), item.get("shareCount", 0)
        created_at = item.get("createTimeISO", "")
        results.append({
            "platform": "tiktok", "hashtag": hashtag, "post_id": item.get("id", ""),
            "text": (item.get("text") or "")[:300],
            "author": item.get("authorMeta", {}).get("name", ""),
            "views": views, "likes": likes, "comments": comments, "shares": shares,
            "created_at": created_at,
            "hashtags": [h.get("name", "") for h in item.get("hashtags", [])],
            "music": item.get("musicMeta", {}).get("musicName", ""),
            "score": round(_engagement_score(views, likes, comments, shares, created_at), 2),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    await _cache_set(cache_key, results)
    return results


@mcp.tool()
async def search_instagram_hashtag(hashtag: str, limit: int = 20) -> list[dict]:
    """
    Scrape Instagram posts for a hashtag, ranked by engagement velocity.

    Args:
        hashtag: Hashtag WITHOUT the # symbol.
        limit:   Max posts (default 20, capped at 50).

    IMPORTANT: apify/instagram-hashtag-scraper has NO top/recent sort — resultsType
    only picks content type (posts/reels/stories). IG's hashtag page returns whatever
    order Instagram gives it (skews recent/low-engagement), so we over-fetch 3x and
    rank client-side.
    """
    hashtag = hashtag.lstrip("#").strip().lower()
    limit = min(limit, 50)
    cache_key = f"fashionos:trend:instagram:{hashtag}:{limit}"

    if (cached := await _cache_get(cache_key)) is not None:
        return cached

    fetch_limit = min(limit * 3, 150)
    run_input = {"hashtags": [hashtag], "resultsType": "posts", "resultsLimit": fetch_limit}

    try:
        raw_items = await _run_actor_sync(INSTAGRAM_ACTOR_ID, run_input, limit=fetch_limit)
    except Exception as exc:
        return [{"error": str(exc), "hashtag": hashtag, "source": "instagram"}]

    results = []
    for item in raw_items:
        likes, comments = item.get("likesCount", 0), item.get("commentsCount", 0)
        created_at = item.get("timestamp", "")
        results.append({
            "platform": "instagram", "hashtag": hashtag, "post_id": item.get("id", ""),
            "shortcode": item.get("shortCode", ""),
            "caption": (item.get("caption") or "")[:300],
            "likes": likes, "comments": comments,
            "post_type": item.get("type", "image"),
            "created_at": created_at, "url": item.get("url", ""),
            "score": round(_engagement_score(0, likes, comments, 0, created_at), 2),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    results = results[:limit]
    await _cache_set(cache_key, results)
    return results


@mcp.tool()
async def get_trending_tiktok_sounds(limit: int = 10) -> list[dict]:
    """
    Extract trending TikTok sounds from Pakistani fashion content.

    Leading trend indicator: sounds go viral 2-3 weeks before products peak.
    On error: [{"error": "...", "source": "tiktok_sounds"}]
    """
    run_input = {
        "hashtags":        ["PakistaniFashion", "GRWM", "OutfitOfTheDay"],
        "resultsPerPage":  limit * 3,
        "maxCrawledItems": limit * 3,
        "proxy": {"useApifyProxy": True},
    }

    try:
        raw_items = await _run_actor_sync(TIKTOK_ACTOR_ID, run_input, limit=limit * 3)
    except Exception as exc:
        return [{"error": str(exc), "source": "tiktok_sounds"}]

    seen: set[str]   = set()
    sounds: list[dict] = []
    for item in raw_items:
        music      = item.get("musicMeta", {})
        music_name = (music.get("musicName") or "").strip()
        if not music_name or music_name in seen:
            continue
        seen.add(music_name)
        sounds.append({
            "name":              music_name,
            "author":            music.get("musicAuthor", ""),
            "is_original":       music.get("musicOriginal", False),
            "sample_post_views": item.get("stats", {}).get("playCount", 0),
        })
        if len(sounds) >= limit:
            break
    return sounds


# ══════════════════════════════════════════════════════════════════════════════
# DM TOOLS — NEW session 7 (Instagram Graph API)
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_instagram_dms(brand_id: str, limit: int = 20) -> list[dict]:
    """
    Get recent Instagram DM conversations that need a reply.

    Fetches conversations via the Messenger Platform API, returns the latest
    message per conversation. Marks each as needs_reply=True if the last
    message is FROM the customer (not from our page).

    Requires in .env:
      INSTAGRAM_PAGE_ID      → Facebook Page ID linked to IG business account
      INSTAGRAM_ACCESS_TOKEN → Long-lived page access token
      App permissions:       instagram_manage_messages, pages_messaging

    Returns list of conversations:
        conversation_id, message_id, user_id, username, message_text,
        created_at, needs_reply, updated_time

    On credential error: [{"error": "...", "source": "instagram_dm"}]

    Used by: DM Agent (Node 1 — fetch_dm_data)
    """

    try:
        creds = await _get_brand_creds(brand_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    
    page_id = creds['instagram_page_id']

    try:
        data = await _ig_get(
            f"{page_id}/conversations",
            {
                "platform": "instagram",
                "fields":   "id,updated_time,participants",
                "limit":    min(limit, 50),
            },
        )
    except Exception as exc:
        return [{"error": str(exc), "source": "instagram_dm"}]

    results = []

    for conv in data.get("data", [])[:limit]:
        conv_id = conv["id"]

        # Get most recent messages in this conversation
        try:
            msg_data = await _ig_get(
                f"{conv_id}/messages",
                {"fields": "id,message,from,created_time", "limit": 3},
            )
        except Exception:
            continue

        messages = msg_data.get("data", [])
        if not messages:
            continue

        latest    = messages[0]
        sender    = latest.get("from", {})
        sender_id = str(sender.get("id", ""))

        # Identify the customer (the participant who isn't us)
        participants = conv.get("participants", {}).get("data", [])
        customer = next(
            (p for p in participants if str(p.get("id")) != str(page_id)),
            {},
        )

        # needs_reply = True if customer sent the last message (not us)
        we_sent_last = (sender_id == str(page_id))

        results.append({
            "conversation_id": conv_id,
            "message_id":      latest.get("id", ""),
            "user_id":         customer.get("id", sender_id),
            "username":        customer.get("username", customer.get("name", "customer")),
            "message_text":    (latest.get("message") or "")[:500],
            "created_at":      latest.get("created_time", ""),
            "needs_reply":     not we_sent_last,
            "updated_time":    conv.get("updated_time", ""),
        })

    return results


@mcp.tool()
async def send_instagram_dm(brand_id: str, user_id: str, reply_text: str) -> dict:
    """
    Send a DM reply to an Instagram user.

    Args:
        user_id:    Customer's Instagram PSID from get_instagram_dms output.
        reply_text: Reply text (max 1000 chars — Instagram limit).

    Returns success confirmation with message_id.
    On error: {"success": false, "error": "...", "user_id": user_id}

    Auto-executed by DM Agent for: size_question, availability, order_status,
    general_inquiry categories. Never auto-sent for: bulk_inquiry, complaint,
    influencer (those are flagged for human review).

    Used by: DM Agent (Node 4 — send_dm_replies)
    """

    try:
        creds = await _get_brand_creds(brand_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    
    token = creds["instagram_access_token"]

    if not token:
        return {"success": False, "error": "INSTAGRAM_ACCESS_TOKEN not configured", "user_id": user_id}

    try:
        result = await _ig_post(
            brand_id,
            "me/messages",
            {
                "recipient": {"id": user_id},
                "message":   {"text": reply_text[:1000]},
            },
        )
        return {
            "success":    True,
            "message_id": result.get("message_id", ""),
            "user_id":    user_id,
            "sent_at":    datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {
            "success":  False,
            "error":    str(exc),
            "user_id":  user_id,
        }


@mcp.tool()
async def get_instagram_comments(
    brand_id: str,
    media_id: str,
    limit:    int = 20,
) -> list[dict]:
    """
    Get comments on a specific Instagram post.

    Args:
        media_id: Instagram media ID (from Instagram Graph API).
                  Get media IDs via: GET /{ig_user_id}/media?fields=id,caption
        limit:    Max comments (default 20, capped at 50).

    Returns: comment_id, media_id, text, username, timestamp, like_count, reply_count
    On error: [{"error": "...", "media_id": media_id, "source": "instagram_comments"}]

    Use for: monitoring product questions on posts before they escalate to DMs.
    Note: Requires the Instagram Business Account ID (not Page ID) for media lookups.

    Used by: DM Agent (optional — comment monitoring enhancement)
    """

    if not media_id or not media_id.strip():
        return [{"error": "media_id is required and cannot be empty", "source": "instagram_comments"}]

    try:
        creds = await _get_brand_creds(brand_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    
    token = creds["instagram_access_token"]

    if not token:
        return [{"error": "INSTAGRAM_ACCESS_TOKEN not configured", "source": "instagram_comments"}]

    try:
        data = await _ig_get(
            f"{media_id}/comments",
            {
                "fields": "id,text,username,timestamp,like_count,reply_count",
                "limit":  min(limit, 50),
            },
        )
    except Exception as exc:
        return [{"error": str(exc), "media_id": media_id, "source": "instagram_comments"}]

    results = []
    for comment in data.get("data", []):
        results.append({
            "comment_id":  comment.get("id", ""),
            "media_id":    media_id,
            "text":        (comment.get("text") or "")[:500],
            "username":    comment.get("username", ""),
            "timestamp":   comment.get("timestamp", ""),
            "like_count":  comment.get("like_count", 0),
            "reply_count": comment.get("reply_count", 0),
        })
    return results


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=8002)