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
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

# ── Apify config (scraping) ───────────────────────────────────────────────────

APIFY_TOKEN    = os.getenv("APIFY_API_TOKEN", "")
APIFY_BASE_URL = "https://api.apify.com/v2"

TIKTOK_ACTOR_ID    = os.getenv("APIFY_TIKTOK_ACTOR_ID",    "clockworks/free-tiktok-scraper")
INSTAGRAM_ACTOR_ID = os.getenv("APIFY_INSTAGRAM_ACTOR_ID", "apify/instagram-hashtag-scraper")
SCRAPE_TIMEOUT     = int(os.getenv("APIFY_SCRAPE_TIMEOUT_SECONDS", "90"))

# ── Instagram Graph API config (DMs) ─────────────────────────────────────────

INSTAGRAM_PAGE_ID      = os.getenv("INSTAGRAM_PAGE_ID", "")
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
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


# ── Apify HTTP helper ─────────────────────────────────────────────────────────

async def _run_actor_sync(actor_id: str, run_input: dict, limit: int = 20) -> list[dict]:
    if not APIFY_TOKEN:
        raise ValueError("APIFY_API_TOKEN not set in social-mcp .env")

    actor_id_url = actor_id.replace("/", "~")
    url    = f"{APIFY_BASE_URL}/acts/{actor_id_url}/run-sync-get-dataset-items"
    params = {"token": APIFY_TOKEN, "maxItems": limit, "clean": "true"}


    async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT + 15) as client:
        r = await client.post(url, json=run_input, params=params)
        r.raise_for_status()
        return r.json()


# ── Instagram Graph API helpers ───────────────────────────────────────────────

async def _ig_get(path: str, params: dict | None = None) -> dict:
    """GET to Instagram Graph API."""
    if not INSTAGRAM_ACCESS_TOKEN:
        raise ValueError("INSTAGRAM_ACCESS_TOKEN not set in social-mcp .env")
    all_params = {"access_token": INSTAGRAM_ACCESS_TOKEN, **(params or {})}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{IG_BASE_URL}/{path}", params=all_params)
        r.raise_for_status()
        return r.json()


async def _ig_post(path: str, payload: dict) -> dict:
    """POST to Instagram Graph API."""
    if not INSTAGRAM_ACCESS_TOKEN:
        raise ValueError("INSTAGRAM_ACCESS_TOKEN not set in social-mcp .env")
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{IG_BASE_URL}/{path}",
            params={"access_token": INSTAGRAM_ACCESS_TOKEN},
            json=payload,
        )
        r.raise_for_status()
        return r.json()


# ══════════════════════════════════════════════════════════════════════════════
# SCRAPING TOOLS (unchanged from session 6)
# ══════════════════════════════════════════════════════════════════════════════

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
        geo:     Country code for geo-targeting (default "PK").

    Returns: platform, hashtag, post_id, text, author, views, likes, comments,
             shares, created_at, hashtags[], music
    On error: [{"error": "...", "hashtag": hashtag, "source": "tiktok"}]
    """
    limit = min(limit, 50)
    run_input = {
        "hashtags":        [hashtag],
        "resultsPerPage":  limit,
        "maxCrawledItems": limit,
        "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
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
    Scrape Instagram posts for a hashtag.

    Args:
        hashtag: Hashtag WITHOUT the # symbol.
        limit:   Max posts (default 20, capped at 50).

    Returns: platform, hashtag, post_id, shortcode, caption, likes, comments,
             post_type, created_at, url
    On error: [{"error": "...", "hashtag": hashtag, "source": "instagram"}]
    """
    limit = min(limit, 50)
    run_input = {
        "hashtags":     [hashtag],
        "resultsLimit": limit,
        "proxy":        {"useApifyProxy": True},
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
            "post_type":  item.get("type", "image"),
            "created_at": item.get("timestamp", ""),
            "url":        item.get("url", ""),
        })
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
async def get_instagram_dms(limit: int = 20) -> list[dict]:
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
    if not INSTAGRAM_PAGE_ID or not INSTAGRAM_ACCESS_TOKEN:
        return [{"error": "INSTAGRAM_PAGE_ID or INSTAGRAM_ACCESS_TOKEN not configured", "source": "instagram_dm"}]

    try:
        data = await _ig_get(
            f"{INSTAGRAM_PAGE_ID}/conversations",
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
            (p for p in participants if str(p.get("id")) != str(INSTAGRAM_PAGE_ID)),
            {},
        )

        # needs_reply = True if customer sent the last message (not us)
        we_sent_last = (sender_id == str(INSTAGRAM_PAGE_ID))

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
async def send_instagram_dm(user_id: str, message: str) -> dict:
    """
    Send a DM reply to an Instagram user.

    Args:
        user_id: Customer's Instagram PSID from get_instagram_dms output.
        message: Reply text (max 1000 chars — Instagram limit).

    Returns success confirmation with message_id.
    On error: {"success": false, "error": "...", "user_id": user_id}

    Auto-executed by DM Agent for: size_question, availability, order_status,
    general_inquiry categories. Never auto-sent for: bulk_inquiry, complaint,
    influencer (those are flagged for human review).

    Used by: DM Agent (Node 4 — send_dm_replies)
    """
    if not INSTAGRAM_ACCESS_TOKEN:
        return {"success": False, "error": "INSTAGRAM_ACCESS_TOKEN not configured", "user_id": user_id}

    try:
        result = await _ig_post(
            "me/messages",
            {
                "recipient": {"id": user_id},
                "message":   {"text": message[:1000]},
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

        
    if not INSTAGRAM_ACCESS_TOKEN:
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