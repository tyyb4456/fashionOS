"""
Inventory Agent — FashionOS Phase 2 Operations
===============================================
Analyses live Shopify inventory, calculates stockout risk per SKU,
flags dead stock anomalies, and writes structured outputs to shared state.

Graph topology  (4 nodes, fully sequential):

    START
      │
      ▼
  fetch_shopify_data       ← Node 1: Opens shopify-mcp via MultiServerMCPClient.
      │                               Calls list_products + calculate_sales_velocity.
      │                               Writes: state.products, state.sales_velocity
      ▼
  load_domain_skill        ← Node 2: Loads fashion_inventory domain skill.
      │                               Pure Python — no I/O.
      │                               Writes: state.skill_content
      ▼
  run_claude_analysis      ← Node 3: Structured LLM call (with_structured_output).
      │                               Input: compact inventory payload + skill prompt.
      │                               Writes: state.raw_analysis (Pydantic JSON)
      ▼
  write_state_outputs      ← Node 4: Deserialises analysis → typed objects.
      │                               Writes: state.inventory_snapshot, state.alerts
      ▼
    END

Subgraph ↔ Parent state mapping:
  - InventoryAgentState shares key names with FashionOSState.
    LangGraph automatically maps matching keys when the subgraph runs inside
    the Supervisor graph.
  - inventory_snapshot + alerts use operator.add → they MERGE into the parent
    state, never overwrite. Safe for future parallel agent execution.
  - skill_content + raw_analysis are agent-internal. They don't exist in
    FashionOSState, so LangGraph ignores them when writing back.

Standalone test:
  python -m agents.inventory.graph
"""

import json
import os
from datetime import datetime, timezone
from typing import Annotated, Optional
import operator

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from langchain.chat_models import init_chat_model

# from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint

from agents.skills import load_skill
from agents.state import AgentAlert, InventorySnapshot

from dotenv import load_dotenv
load_dotenv()


# ── Config ─────────────────────────────────────────────────────────────────────

SHOPIFY_MCP_URL = os.getenv("SHOPIFY_MCP_URL", "http://localhost:8001/mcp")


# llm = HuggingFaceEndpoint(
#     repo_id="deepseek-ai/DeepSeek-R1-0528",
#     task="text-generation",
#     max_new_tokens=4096,      # 512 would cut off mid-JSON
#     do_sample=False,
#     repetition_penalty=1.03,
#     provider="auto",
#     timeout=600,              # 10 minutes
# )

# chat_model = ChatHuggingFace(llm=llm)



model = init_chat_model("google_genai:gemini-2.5-flash-lite")


# ── Pydantic output schema ─────────────────────────────────────────────────────
# Used with llm.with_structured_output() — Claude fills these via tool_use blocks.
# LangChain parses and validates automatically. No fragile regex JSON extraction.

class _SnapshotOut(BaseModel):
    """One row per variant SKU."""
    sku:                     str
    product_title:           str
    variant_title:           str
    current_stock:           int  = Field(ge=0)
    units_per_day:           float = Field(ge=0.0)
    days_of_stock_remaining: float = Field(
        description=(
            "current_stock / units_per_day. "
            "Set to 999.0 for zero-velocity SKUs (no sales in window)."
        )
    )
    urgency: str = Field(
        description=(
            'Exactly one of: "critical" (<7 days), "high" (7–14 days), '
            '"normal" (14–30 days or zero-velocity), "healthy" (>30 days).'
        )
    )


class _AlertOut(BaseModel):
    level:   str = Field(description='One of: "critical", "warning", "info"')
    message: str = Field(description="Human-readable alert. Be specific — include SKU, numbers, urgency.")
    sku:     Optional[str] = Field(default=None, description="SKU this alert relates to, if applicable.")


class _InventoryAnalysis(BaseModel):
    """Complete structured output the Inventory Agent produces."""
    inventory_snapshots: list[_SnapshotOut] = Field(
        description="One entry per active variant SKU. Include ALL variants."
    )
    alerts: list[_AlertOut] = Field(
        description=(
            "Raise only actionable alerts. "
            "critical = stockout < 7 days. "
            "warning  = dead stock (stock > 0, zero velocity 14+ days). "
            "info     = size distribution anomaly (L/XL outselling S/M)."
        )
    )
    summary: str = Field(
        description=(
            "2–3 sentence overview of overall inventory health. "
            "Example: '14 SKUs healthy. 2 CRITICAL (restock in <7 days). "
            "3 dead stock variants flagged for markdown review.'"
        )
    )


# ── Subgraph State ─────────────────────────────────────────────────────────────

class InventoryAgentState(TypedDict):
    """
    Local state for the Inventory Agent subgraph.

    Design decisions:
    - Only carries FashionOSState fields this agent actually reads or writes.
      Everything else in the parent state is untouched.
    - inventory_snapshot + alerts use operator.add so outputs from multiple
      agents (when running in parallel later) are MERGED, not overwritten.
    - skill_content and raw_analysis are agent-internal scratch space.
      They don't exist in FashionOSState — LangGraph drops them on merge.
    """
    # ── From parent state (read-only context) ────────────────────────────────
    brand_id:   str
    brand_name: str

    # ── Populated by Node 1 ──────────────────────────────────────────────────
    products:       list[dict]   # Raw Shopify product list with variants
    sales_velocity: list[dict]   # units_per_day per SKU, 14-day window

    # ── Agent-internal scratch ───────────────────────────────────────────────
    skill_content: str   # Loaded in Node 2, consumed in Node 3
    raw_analysis:  str   # Pydantic JSON string from Node 3, parsed in Node 4

    # ── Final outputs → merged into parent FashionOSState ───────────────────
    inventory_snapshot: Annotated[list[InventorySnapshot], operator.add]
    alerts:             Annotated[list[AgentAlert],        operator.add]


# ── Helper: parse MCP tool response ───────────────────────────────────────────

def _parse_mcp_result(raw) -> list | dict:
    # LangChain content block: [{'type': 'text', 'text': '[...json...]', 'id': '...'}]
    if (
        isinstance(raw, list)
        and len(raw) > 0
        and isinstance(raw[0], dict)
        and "text" in raw[0]
    ):
        return json.loads(raw[0]["text"])

    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    # ToolMessage or similar
    content = getattr(raw, "content", str(raw))
    if isinstance(content, str):
        return json.loads(content)
    return content


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — fetch_shopify_data
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_shopify_data(state: InventoryAgentState) -> dict:
    """
    Opens a connection to shopify-mcp and fetches the raw data this agent needs.

    Calls two MCP tools:
      list_products(limit=250)         → all active products + variants + stock
      calculate_sales_velocity(days=14) → units/day per SKU over last 14 days

    Uses langchain-mcp-adapters MultiServerMCPClient which:
      - Connects to the FastMCP server via streamable-http transport
      - Converts MCP tool definitions into standard LangChain StructuredTools
      - Handles the MCP session handshake automatically

    The context manager closes the MCP connection cleanly after the node exits.
    """
    client = MultiServerMCPClient(
        {
            "shopify": {
                "url":       SHOPIFY_MCP_URL,
                "transport": "streamable_http",
            }
        }
    )
    tools = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    products_raw = await tool_map["list_products"].ainvoke(
        {"limit": 250, "status": "active"}
    )
    velocity_raw = await tool_map["calculate_sales_velocity"].ainvoke(
        {"days": 14}
    )

    products  = _parse_mcp_result(products_raw)
    velocity  = _parse_mcp_result(velocity_raw)

    print(
        f"[Inventory] Fetched {len(products)} products, "
        f"{len(velocity)} velocity records from Shopify."
    )

    return {
        "products":       products,
        "sales_velocity": velocity,
    }


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — load_domain_skill
# ══════════════════════════════════════════════════════════════════════════════

def load_domain_skill(state: InventoryAgentState) -> dict:
    """
    Loads the fashion_inventory domain skill into state.

    This is the progressive-disclosure pattern: the agent's base system prompt
    is intentionally thin. Domain knowledge (dead stock thresholds, Pakistani
    size ratios, supplier lead times, urgency formulas) is injected here and
    included in the Node 3 prompt — keeping base prompts lean across all agents.
    """
    skill = load_skill("fashion_inventory")
    print("[Inventory] Domain skill loaded.")
    return {"skill_content": skill}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — run_claude_analysis
# ══════════════════════════════════════════════════════════════════════════════

async def run_claude_analysis(state: InventoryAgentState) -> dict:
    """
    Calls Claude with a structured output schema to analyse inventory data.

    Design choices:
    - NOT a ReAct agent loop. The data is already in state from Node 1.
      A single structured call is faster, cheaper, and sufficient here.
      (ReAct pattern is reserved for agents needing dynamic multi-step tool use,
       e.g. Trend Agent scraping multiple sources, Restock Agent drafting orders.)
    - llm.with_structured_output(_InventoryAnalysis) forces Claude to respond
      via tool_use blocks. LangChain validates + parses into the Pydantic model.
      No free-text JSON parsing. No regex. No hallucinated schema drift.
    - We build a compact inventory payload (pre-computed days_estimated) to
      reduce token usage and give Claude clean data to reason over.
    """
    # ── Build velocity lookup for O(1) joins ──────────────────────────────────
    velocity_by_sku: dict[str, float] = {
        v["sku"]: v["units_per_day"]
        for v in state.get("sales_velocity", [])
        if v.get("sku")
    }

    # ── Build compact inventory payload ──────────────────────────────────────
    # Flatten products → variants, pre-compute days_estimated, skip no-SKU variants
    compact: list[dict] = []
    for product in state.get("products", []):
        for variant in product.get("variants", []):
            sku = (variant.get("sku") or "").strip()
            if not sku:
                continue  # skip variants with no SKU assigned

            stock    = variant.get("inventory_quantity", 0)
            velocity = velocity_by_sku.get(sku, 0.0)

            # Pre-compute to help Claude verify — it can override if logic differs
            days_estimated = round(stock / velocity, 1) if velocity > 0 else 999.0

            compact.append({
                "sku":            sku,
                "product_title":  product.get("title", ""),
                "variant_title":  variant.get("title", ""),
                "current_stock":  stock,
                "units_per_day":  velocity,
                "days_estimated": days_estimated,
                # Flag zero-velocity for Claude to notice
                "zero_velocity":  velocity == 0.0 and stock > 0,
            })

    if not compact:
        # Edge case: store has no active products or all variants lack SKUs
        print("[Inventory] WARNING: No SKU data found. Check Shopify product setup.")
        empty = _InventoryAnalysis(
            inventory_snapshots=[],
            alerts=[_AlertOut(
                level="warning",
                message="No active SKUs found in Shopify. Ensure products have SKUs assigned.",
                sku=None,
            )],
            summary="No active SKUs found. Store may be empty or SKUs unassigned.",
        )
        return {"raw_analysis": empty.model_dump_json()}

    # ── Prompts ───────────────────────────────────────────────────────────────
    system_prompt = f"""You are the Inventory Agent for {state['brand_name']}, \
an autonomous AI fashion brand system.

{state['skill_content']}

## Your task
Analyse the inventory snapshot below and classify every SKU. Apply the stockout
prediction formula and urgency thresholds from your domain skill exactly.

Special patterns to flag:
1. CRITICAL alert  → days_remaining < 7. Restock order must go TODAY.
2. HIGH alert      → days_remaining 7–14. Order within 3 days.
3. Dead stock      → stock > 0, units_per_day = 0. Raise a "warning" alert.
                     Set urgency = "normal" (not critical — they're not selling out).
4. Size anomaly    → If L/XL variant velocity > S/M variant velocity for the
                     same product, raise an "info" alert (sizing runs large).

Rules:
- Analyse EVERY variant as its own entry — do not group sizes together.
- Use days_estimated as the starting value but verify it matches the formula.
- Only raise alerts that require human attention or action.
- Keep alert messages specific: include SKU, numbers, and what action is needed.
"""

    user_message = (
        f"Here is the current inventory + velocity data for {state['brand_name']}:\n\n"
        f"```json\n{json.dumps(compact, indent=2)}\n```\n\n"
        "Analyse every SKU and return your complete structured inventory analysis."
    )

    # ── Structured LLM call ───────────────────────────────────────────────────
    import re

    structured_llm = model.with_structured_output(
        _InventoryAnalysis,
        # method="json_schema",
        # include_raw=True,         # get raw response so we can inspect it
    )

    # raw_result = await structured_llm.ainvoke([
    #     SystemMessage(content=system_prompt),
    #     HumanMessage(content=user_message),
    # ])

    # # strip <think>...</think> then re-parse if parsing failed
    # if raw_result.get("parsing_error"):
    #     raw_text = raw_result["raw"].content
    #     clean = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
    #     analysis = _InventoryAnalysis.model_validate_json(clean)
    # else:
    #     analysis = raw_result["parsed"]

    analysis: _InventoryAnalysis = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_message),
    ])


    print(
        f"[Inventory] Analysis complete. "
        f"{len(analysis.inventory_snapshots)} snapshots, "
        f"{len(analysis.alerts)} alerts. "
        f"Summary: {analysis.summary}"
    )

    return {"raw_analysis": analysis.model_dump_json()}


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — write_state_outputs
# ══════════════════════════════════════════════════════════════════════════════

def write_state_outputs(state: InventoryAgentState) -> dict:
    """
    Deserialises the validated Pydantic JSON from Node 3 into typed
    InventorySnapshot and AgentAlert dicts that conform to FashionOSState.

    These lists are written with operator.add semantics — they will be MERGED
    into the parent state when the Supervisor collects agent outputs.
    This means future parallel agents (Trend, Pricing, etc.) can all write to
    state.alerts without clobbering each other.
    """
    analysis = _InventoryAnalysis.model_validate_json(state["raw_analysis"])
    now_iso  = datetime.now(timezone.utc).isoformat()

    # ── Build typed InventorySnapshot list ────────────────────────────────────
    inventory_snapshot: list[InventorySnapshot] = [
        InventorySnapshot(
            sku=s.sku,
            product_title=s.product_title,
            variant_title=s.variant_title,
            current_stock=s.current_stock,
            units_per_day=s.units_per_day,
            days_of_stock_remaining=s.days_of_stock_remaining,
            urgency=s.urgency,
        )
        for s in analysis.inventory_snapshots
    ]

    # ── Build typed AgentAlert list ───────────────────────────────────────────
    alerts: list[AgentAlert] = [
        AgentAlert(
            level=a.level,
            agent="inventory_agent",
            message=a.message,
            sku=a.sku,
            created_at=now_iso,
        )
        for a in analysis.alerts
    ]

    print(
        f"[Inventory] Written {len(inventory_snapshot)} snapshots "
        f"and {len(alerts)} alerts to state."
    )

    return {
        "inventory_snapshot": inventory_snapshot,
        "alerts":             alerts,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_inventory_graph() -> StateGraph:
    """
    Assembles and compiles the Inventory Agent subgraph.

    Returns a compiled LangGraph that can be used two ways:

    1. Standalone (for testing + Celery workers):
       result = await inventory_graph.ainvoke(initial_state)

    2. As a subgraph inside the Supervisor:
       supervisor_graph.add_node("inventory_agent", inventory_graph)
       # LangGraph auto-maps parent FashionOSState ↔ InventoryAgentState by key names
    """
    graph = StateGraph(InventoryAgentState)

    # Register nodes
    graph.add_node("fetch_shopify_data",  fetch_shopify_data)
    graph.add_node("load_domain_skill",   load_domain_skill)
    graph.add_node("run_claude_analysis", run_claude_analysis)
    graph.add_node("write_state_outputs", write_state_outputs)

    # Wire edges — purely sequential for now
    # (Future: add conditional edges for error recovery, retry logic, etc.)
    graph.add_edge(START,                  "fetch_shopify_data")
    graph.add_edge("fetch_shopify_data",   "load_domain_skill")
    graph.add_edge("load_domain_skill",    "run_claude_analysis")
    graph.add_edge("run_claude_analysis",  "write_state_outputs")
    graph.add_edge("write_state_outputs",  END)

    return graph.compile()


# Module-level compiled graph — import this everywhere
# from agents.inventory import inventory_graph
inventory_graph = build_inventory_graph()


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test runner
# python -m agents.inventory.graph
# (requires SHOPIFY_MCP_URL pointing at a live shopify-mcp instance)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()

    async def _test_run():
        print("\n" + "═" * 60)
        print("  FashionOS — Inventory Agent Test Run")
        print("═" * 60 + "\n")

        # Minimal initial state — supervisor will fill brand_id/brand_name in prod
        initial_state: InventoryAgentState = {
            "brand_id":           os.getenv("BRAND_ID", "test-brand-001"),
            "brand_name":         os.getenv("BRAND_NAME", "TestBrand"),
            "products":           [],
            "sales_velocity":     [],
            "skill_content":      "",
            "raw_analysis":       "",
            "inventory_snapshot": [],
            "alerts":             [],
        }

        result = await inventory_graph.ainvoke(initial_state)

        print("\n── INVENTORY SNAPSHOT ─────────────────────────────────────────")
        for snap in sorted(
            result["inventory_snapshot"],
            key=lambda s: s["days_of_stock_remaining"]
        ):
            print(
                f"  {snap['sku']:<20} "
                f"{snap['urgency'].upper():<10} "
                f"{snap['days_of_stock_remaining']:>6.1f} days  "
                f"({snap['current_stock']} units @ {snap['units_per_day']}/day)"
            )

        print("\n── ALERTS ─────────────────────────────────────────────────────")
        for alert in result["alerts"]:
            sku_tag = f" [{alert['sku']}]" if alert.get("sku") else ""
            print(f"{alert['level'].upper()}{sku_tag}: {alert['message']}")

        print("\n── DONE ───────────────────────────────────────────────────────\n")

    asyncio.run(_test_run())