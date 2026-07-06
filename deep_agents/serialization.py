"""
FashionOS Deep Agent — Serialization Patches
==============================================
Two patches needed so Pydantic response models (from response_schemas/*) can be
safely written into and read back from the LangGraph Redis checkpointer.

NOTE: these were needed when deep_agents/subagents/ produced structured Pydantic
output (InventoryAnalysis, TrendAnalysis, etc.) directly inside the conversation's
message history. Since subagents have been removed in favor of
start_agent_analysis / check_agent_analysis_status (which return plain dicts —
Celery already JSON-serializes everything before it gets anywhere near this
checkpointer), nothing in the current message flow should need this allowlist
anymore. Left in place because it's inert if unused, and safer than assuming no
code path anywhere still serializes one of these types into a checkpoint. Safe
to delete later once confirmed dead in production.

Import this module for its side effects before building the checkpointer:
    from deep_agents import serialization  # noqa: F401
"""

from pydantic import BaseModel as _PydanticBase

# ── Patch 1 — Redis serializer default handler ────────────────────────────────
try:
    from langgraph.checkpoint.redis.jsonplus_redis import JsonPlusRedisSerializer

    _orig_default_handler = JsonPlusRedisSerializer._default_handler

    def _patched_default_handler(self, obj):
        if isinstance(obj, _PydanticBase):
            return obj.model_dump()
        return _orig_default_handler(self, obj)

    JsonPlusRedisSerializer._default_handler = _patched_default_handler
    print("[FashionOS] ✓ JsonPlusRedisSerializer patched")
except Exception as e:
    print(f"[FashionOS] ⚠ Redis serializer patch failed: {e}")


# ── Patch 2 — LangGraph's custom-Pydantic-type allowlist ──────────────────────
# Format: [(module_path, classname), ...] — exactly as LangGraph's own warning
# message specifies. Historically needed for each subagent's response_format
# schema (see response_schemas/*.py) — kept for safety, see module docstring.
FASHIONOS_ALLOWED_MSGPACK: list[tuple[str, str]] = [
    ("response_schemas.inventory_model", "InventoryAnalysis"),
    ("response_schemas.inventory_model", "SnapshotOut"),
    ("response_schemas.inventory_model", "AlertOut"),
    ("response_schemas.trend_model",     "TrendAnalysis"),
    ("response_schemas.trend_model",     "TrendSignalOut"),
    ("response_schemas.trend_model",     "TrendAlertOut"),
    ("response_schemas.pricing_model",   "PricingAnalysis"),
    ("response_schemas.pricing_model",   "PricingDecisionOut"),
    ("response_schemas.marketing_model", "MarketingAnalysis"),
    ("response_schemas.marketing_model", "CampaignDecisionOut"),
    ("response_schemas.restock_model",   "RestockAnalysis"),
    ("response_schemas.restock_model",   "RestockDecisionOut"),
    ("response_schemas.restock_model",   "SupplierBatch"),
    ("response_schemas.content_model",   "ContentPlan"),
    ("response_schemas.content_model",   "ContentPostOut"),
    ("response_schemas.content_model",   "ContentFatigueSkip"),
    ("response_schemas.content_model",   "InstagramOut"),
    ("response_schemas.content_model",   "TikTokOut"),
    ("response_schemas.content_model",   "ShotListItem"),
]