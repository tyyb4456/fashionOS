"""
ads-mcp — FashionOS MCP Server
Exposes Meta Marketing API (Facebook + Instagram Ads) as MCP tools.

Read tools:
  get_campaigns             → all campaigns with status + daily budget
  get_campaign_performance  → insights (ROAS, spend, CTR) for a campaign

Write tools:
  update_campaign_budget    → change daily budget in PKR
  pause_campaign            → pause a running campaign
  activate_campaign         → resume a paused campaign

Budget representation:
  Meta stores budgets in the smallest currency unit.
  For PKR: API value = PKR × 100 (PKR 500 → 50000 in API).
  All tools accept and return PKR — this server handles the conversion.

Campaign naming convention:
  FashionOS_{SKU}_{short_description}
  Example: FashionOS_FOS-001-S_OliveCargo
  The Marketing Agent uses this to map campaigns → SKUs automatically.
  Campaigns not following this convention get matched_sku = null and a "hold" decision.

Port: 8004
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

# ── Multi-tenant credential fetching ─────────────────────────────────────────
# MCP servers are shared across all brands.
# Each tool receives brand_id and fetches credentials from Redis.

import redis.asyncio as _aioredis

_REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")


async def _get_brand_creds(brand_id: str) -> dict:
    """
    Fetch decrypted brand credentials from Redis.
    The main API writes these when a brand is created or credentials are updated.
    Raises ValueError if brand_id is not found in cache — caller returns an error response.
    """
    r = _aioredis.from_url(_REDIS_URL, decode_responses=True)
    try:
        raw = await r.get(f"fashionos:creds:{brand_id}")
        if not raw:
            raise ValueError(
                f"No credentials found for brand_id='{brand_id}'. "
                "Ensure the brand exists and POST /api/v1/brands was called first."
            )
        import json as _json
        return _json.loads(raw)
    finally:
        await r.aclose()

# ── Config ────────────────────────────────────────────────────────────────────

GRAPH_API_VERSION  = os.getenv("META_GRAPH_API_VERSION", "v21.0")
BASE_URL           = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# PKR has minor units in Meta's representation: 1 PKR = 100 paise (API value)
BUDGET_DIVISOR = 100

# ── FastMCP app ───────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="ads-mcp",
    instructions=(
        "Access Meta Marketing API for Facebook and Instagram ad campaign management. "
        "All budget values are in PKR (Pakistani Rupees). "
        "Campaign naming convention: FashionOS_{SKU}_{description}. "
        "The Marketing Agent reads trends + inventory + pricing from FashionOSState and "
        "uses these tools to optimise ad spend accordingly."
    ),
)

async def _meta_get(brand_id: str, path: str, params: dict | None = None) -> dict:
    creds = await _get_brand_creds(brand_id)
    token = creds["meta_access_token"]
    all_params = {"access_token": token, **(params or {})}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{BASE_URL}/{path}", params=all_params)
        r.raise_for_status()
        return r.json()

async def _meta_post(brand_id: str, path: str, data: dict) -> dict:
    creds = await _get_brand_creds(brand_id)
    token = creds["meta_access_token"]
    all_data = {"access_token": token, **data}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{BASE_URL}/{path}", data=all_data)
        r.raise_for_status()
        return r.json()


def _budget_to_pkr(api_value: str | int | None) -> float:
    """Convert Meta API budget value → PKR float. Returns 0 if None/missing."""
    if api_value is None:
        return 0.0
    try:
        return float(api_value) / BUDGET_DIVISOR
    except (ValueError, TypeError):
        return 0.0


def _pkr_to_budget(pkr: float) -> int:
    """Convert PKR float → Meta API budget int."""
    return int(round(pkr * BUDGET_DIVISOR))


def _ensure_act_prefix(account_id: str) -> str:
    """Ensure the ad account ID has the 'act_' prefix Meta requires."""
    return account_id if account_id.startswith("act_") else f"act_{account_id}"


# ── TOOLS ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_campaigns(brand_id: str, active_only: bool = True) -> list[dict]:
    """
    List all campaigns for the configured Meta ad account.

    Args:
        brand_id: The brand ID for which to fetch campaigns.
        active_only: If True, returns only ACTIVE and PAUSED campaigns.
                     If False, includes DELETED and ARCHIVED too.

    Returns list of campaigns. Each campaign dict has:
        campaign_id, name, status, effective_status, daily_budget_pkr,
        has_daily_budget, objective, created_time

    Note on daily_budget_pkr:
      Value is 0 if the campaign uses ad-set level budgets (CBO off).
      The Marketing Agent skips budget changes for campaigns with has_daily_budget=False —
      it can still pause/activate them but cannot adjust budget at campaign level.

    On credential error or API failure:
        Returns [{"error": "...", "source": "meta_ads"}]

    Used by: Marketing Agent (Node 1 — fetch_campaign_data)
    """
    try:
        creds = await _get_brand_creds(brand_id)
    except ValueError as e:
        return [{"error": str(e), "source": "meta_ads"}]
    account_id = _ensure_act_prefix(creds["meta_ad_account_id"])


    # Meta expects effective_status as a JSON array string
    statuses = ["ACTIVE", "PAUSED"] if active_only else ["ACTIVE", "PAUSED", "ARCHIVED"]

    try:
        data = await _meta_get(
            brand_id,
            f"{account_id}/campaigns",
            {
                "fields":           "id,name,status,effective_status,daily_budget,"
                                    "lifetime_budget,objective,created_time",
                "effective_status": json.dumps(statuses, separators=(',', ':')),
                "limit":            100,
            },
        )
    except Exception as exc:
        return [{"error": str(exc), "source": "meta_ads"}]

    results = []
    for c in data.get("data", []):
        daily_budget_pkr = _budget_to_pkr(c.get("daily_budget"))
        results.append({
            "campaign_id":       str(c["id"]),
            "name":              c.get("name", ""),
            "status":            c.get("status", ""),
            "effective_status":  c.get("effective_status", ""),
            "daily_budget_pkr":  daily_budget_pkr,
            "has_daily_budget":  daily_budget_pkr > 0,
            "objective":         c.get("objective", ""),
            "created_time":      c.get("created_time", ""),
        })

    return results


@mcp.tool()
async def get_campaign_performance(
    brand_id,
    campaign_id: str,
    days:        int = 7,
) -> dict:
    """
    Get performance metrics for a specific campaign over the last N days.

    Args:
        campaign_id: Meta campaign ID (from get_campaigns output).
        days:        Look-back period. One of: 1, 7, 14, 30. Default 7.

    Returns performance metrics:
        spend_pkr, impressions, clicks, ctr, cpc_pkr,
        purchase_roas (None if Meta Pixel not configured or no conversions tracked),
        reach, no_data (True if campaign had no spend in this period)

    ROAS note:
      purchase_roas is only available if the Meta Pixel is installed on the
      Shopify store and Purchase events are being tracked. Without it, use
      spend + CTR as the primary efficiency signals. The Marketing Agent
      handles None ROAS gracefully — it falls back to inventory + trend signals.

    On error: returns {"error": "...", "campaign_id": campaign_id}

    Used by: Marketing Agent (Node 1 — called per active campaign with daily budget)
    """
    date_preset_map = {1: "today", 7: "last_7_days", 14: "last_14_days", 30: "last_30_days"}
    date_preset = date_preset_map.get(days, "last_7_days")

    try:
        data = await _meta_get(
            brand_id,
            f"{campaign_id}/insights",
            {
                "fields":      "spend,impressions,clicks,ctr,cpc,purchase_roas,reach",
                "date_preset": date_preset,
                "level":       "campaign",
            },
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400:
            return {
                "campaign_id":   campaign_id,
                "date_preset":   date_preset,
                "spend_pkr":     0.0,
                "impressions":   0,
                "clicks":        0,
                "ctr":           0.0,
                "cpc_pkr":       0.0,
                "purchase_roas": None,
                "reach":         0,
                "no_data":       True,
            }
        return {"error": str(exc), "campaign_id": campaign_id}
    except Exception as exc:
        return {"error": str(exc), "campaign_id": campaign_id}

    if not data.get("data"):
        # No spend in this period — campaign was inactive or just created
        return {
            "campaign_id":   campaign_id,
            "date_preset":   date_preset,
            "spend_pkr":     0.0,
            "impressions":   0,
            "clicks":        0,
            "ctr":           0.0,
            "cpc_pkr":       0.0,
            "purchase_roas": None,
            "reach":         0,
            "no_data":       True,
        }

    row = data["data"][0]

    # purchase_roas is a list of action-value dicts
    roas: Optional[float] = None
    for r in (row.get("purchase_roas") or []):
        if r.get("action_type") in ("omni_purchase", "purchase"):
            try:
                roas = float(r["value"])
                break
            except (ValueError, KeyError):
                pass

    return {
        "campaign_id":   campaign_id,
        "date_preset":   date_preset,
        "spend_pkr":     float(row.get("spend", 0)),
        "impressions":   int(row.get("impressions", 0)),
        "clicks":        int(row.get("clicks", 0)),
        "ctr":           float(row.get("ctr", 0)),
        "cpc_pkr":       float(row.get("cpc", 0)),
        "purchase_roas": roas,
        "reach":         int(row.get("reach", 0)),
        "no_data":       False,
    }


@mcp.tool()
async def update_campaign_budget(
    brand_id,
    campaign_id:          str,
    new_daily_budget_pkr: float,
    reason:               str,
) -> dict:
    """
    Update the daily budget for a campaign.

    Args:
        campaign_id:          Meta campaign ID.
        new_daily_budget_pkr: New daily budget in PKR. Minimum PKR 200.
        reason:               Why this change is being made — for the audit log.

    Returns success confirmation with the new budget value.

    Caveats:
      - Budget changes take effect within ~15 minutes in Meta's system.
      - Changing budget by >20% in either direction triggers a learning phase
        reset, temporarily reducing performance for 1-3 days.
      - The Marketing Agent caps changes at ±30% per cycle to limit resets.
      - If new_daily_budget_pkr < PKR 200: returns error, use pause_campaign instead.

    Used by: Marketing Agent (Node 4 — auto-execute for decrease, pending for increase)
    """
    if new_daily_budget_pkr < 200:
        return {
            "success":     False,
            "error":       f"PKR {new_daily_budget_pkr:.0f} is below the PKR 200 minimum. Use pause_campaign instead.",
            "campaign_id": campaign_id,
        }

    api_budget = _pkr_to_budget(new_daily_budget_pkr)

    try:
        result = await _meta_post(brand_id, campaign_id, {"daily_budget": api_budget})
        return {
            "success":              True,
            "campaign_id":          campaign_id,
            "new_daily_budget_pkr": new_daily_budget_pkr,
            "api_budget_value":     api_budget,
            "reason":               reason,
            "meta_confirmed":       result.get("success", True),
        }
    except Exception as exc:
        return {
            "success":     False,
            "error":       str(exc),
            "campaign_id": campaign_id,
        }


@mcp.tool()
async def pause_campaign(brand_id, campaign_id: str, reason: str) -> dict:
    """
    Pause a running Meta campaign immediately.

    Args:
        campaign_id: Meta campaign ID.
        reason:      Why the campaign is being paused — for the audit log.

    Returns success confirmation.

    Auto-executed by the Marketing Agent when:
      - matched SKU is out of stock (current_stock < 5)
      - matched SKU has action=clearance_code from Pricing Agent
      - 7-day ROAS < 0.8 AND spend > PKR 500 (burning money)

    Paused campaigns retain their audience learning and historical data.
    Reactivating is much faster than creating a new campaign from scratch.

    Used by: Marketing Agent (Node 4 — auto-execute for out-of-stock + clearance)
    """
    try:
        result = await _meta_post(brand_id, campaign_id, {"status": "PAUSED"})
        return {
            "success":      True,
            "campaign_id":  campaign_id,
            "new_status":   "PAUSED",
            "reason":       reason,
            "meta_confirmed": result.get("success", True),
            "paused_at":    datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {
            "success":     False,
            "error":       str(exc),
            "campaign_id": campaign_id,
        }


@mcp.tool()
async def activate_campaign(brand_id, campaign_id: str, reason: str) -> dict:
    """
    Resume a paused Meta campaign.

    Args:
        campaign_id: Meta campaign ID.
        reason:      Why the campaign is being activated — for the audit log.

    Returns success confirmation.

    This tool is ONLY called after explicit human approval in the dashboard.
    The Marketing Agent always marks 'activate' as pending_approval — it never
    auto-activates a paused campaign.

    Used by: Marketing Agent (Node 4 — pending_approval path only)
    """
    try:
        result = await _meta_post(brand_id, campaign_id, {"status": "ACTIVE"})
        return {
            "success":       True,
            "campaign_id":   campaign_id,
            "new_status":    "ACTIVE",
            "reason":        reason,
            "meta_confirmed":result.get("success", True),
            "activated_at":  datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {
            "success":     False,
            "error":       str(exc),
            "campaign_id": campaign_id,
        }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=8004)