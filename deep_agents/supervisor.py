"""
FashionOS Deep Agent Supervisor
================================
Conversational orchestrator built with the deepagents harness.

Tool layers:
1. DB tools   — read latest pipeline results from PostgreSQL (instant, no API calls)
2. task tool  — spawn specialist subagents (inventory-agent, etc.) for live data
3. file tools — read SKILL.md procedures, write reports to workspace/

Why DB tools on the supervisor directly (not a DB subagent):
DB reads are fast, simple queries — no multi-step reasoning required.
A DB subagent would add an extra LLM call (the task delegation) for what is
essentially a SELECT statement. Direct tools return data instantly.

Use subagents only when the work inside is complex enough to need its own
reasoning loop (e.g., inventory-agent making multiple MCP calls + LLM analysis).

When to use DB tools vs inventory-agent subagent:
DB tools        → "what happened in the last run?", "what's pending?", "show me returns"
                  Fast: reading already-processed results from the pipeline
inventory-agent → "run a fresh inventory check", "what's actually in stock right now?"
                  Slow: live Shopify API calls + LLM analysis (~30s)
"""

import os
import asyncio
from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_mcp_adapters.client import MultiServerMCPClient
from deep_agents.subagents.inventory_agent import build_inventory_subagent
from deep_agents.tools.db_tools import get_db_tools
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
SHOPIFY_MCP_URL = os.getenv("SHOPIFY_MCP_URL", "http://localhost:8001/mcp")
BASE_DIR        = Path(__file__).parent.resolve()

# ── System Prompt ──────────────────────────────────────────────────────────────
SUPERVISOR_PROMPT = """
You are FashionOS Supervisor — the autonomous AI brain of a Pakistani Shopify fashion brand. You answer the founder's questions, run agent pipelines, and synthesise results into clear, number-backed decisions.

## Tool strategy

### For questions about what already happened (fast — use DB tools):
- "What's the inventory status?"      → get_inventory_status()
- "What needs my approval?"           → get_pending_approvals()
- "What alerts are open?"             → get_open_alerts()
- "How has the store been doing?"     → get_run_history()
- "Tell me about SKU FOS-042-S"       → get_sku_history()
- "When did FashionOS last run?"      → get_pipeline_status()
- "What should I post today?"         → get_content_queue()
- "Why are people returning my dress?"→ get_return_insights()

### For live fresh analysis (slower — use subagents):
- "Run a fresh inventory check"       → task(name="inventory-agent", ...)
- "What's actually in stock right now?" → task(name="inventory-agent", ...)

### For procedures (read skills first):
- Before delegating a complex sweep, read the matching SKILL.md via read_file
- Skills tell you the exact step-by-step procedure to follow or delegate

## Output format
Always structure your response as:
🔴 CRITICAL  (action needed today)
🟡 WARNING   (action needed this week)
🟢 HEALTHY   (no action needed)

- One bullet per SKU or decision. Include numbers: stock, velocity, days remaining, PKR.
- End with: "X items pending your approval in the dashboard." if approvals exist.

## Rules
1. Never call Shopify or Meta APIs directly — subagents do that.
2. Never guess at numbers — call a tool first.
3. Don't run the inventory subagent for questions the DB tools already answer.
4. If data doesn't exist, say "The pipeline hasn't run yet" — not "I don't have access."
"""

# ── Supervisor factory ─────────────────────────────────────────────────────────
async def build_supervisor():
    """
    Builds and returns the compiled FashionOS supervisor.
    Tool layers:
    - DB tools (9 functions) : instant PostgreSQL reads of pipeline results
    - inventory-agent        : live Shopify MCP calls + LLM structured analysis
    - FilesystemBackend      : skills catalog (SKILL.md) + workspace for reports
    - AGENTS.md              : always in context (brand rules, thresholds, market)
    """
    # ── Connect to MCP — tools passed down to subagents, not supervisor ────────
    client = MultiServerMCPClient(
        {"shopify": {"url": SHOPIFY_MCP_URL, "transport": "streamable_http"}}
    )
    mcp_tools = await client.get_tools()

    # ── Subagents ──────────────────────────────────────────────────────────────
    inventory_subagent = await build_inventory_subagent(mcp_tools)

    # Future — uncomment as built:
    # trend_subagent     = await build_trend_subagent(mcp_tools)
    # pricing_subagent   = await build_pricing_subagent(mcp_tools)
    # restock_subagent   = await build_restock_subagent(mcp_tools)
    # content_subagent   = await build_content_subagent(mcp_tools)
    # returns_subagent   = await build_returns_subagent(mcp_tools)
    # marketing_subagent = await build_marketing_subagent(mcp_tools)
    # dm_subagent        = await build_dm_subagent(mcp_tools)

    # ── Build supervisor ───────────────────────────────────────────────────────
    agent = create_deep_agent(
        name          = "fashionos-supervisor",
        model         = "google_genai:gemini-2.5-flash-lite",
        system_prompt = SUPERVISOR_PROMPT,
        # DB tools wired directly — fast reads, no subagent overhead
        tools         = get_db_tools(),
        # Subagents — for live data + complex multi-step analysis
        subagents     = [inventory_subagent],
        # Filesystem — skills catalog + workspace/
        backend       = FilesystemBackend(root_dir=str(BASE_DIR)),
        # AGENTS.md always injected into context
        memory        = [str(BASE_DIR / "AGENTS.md")],
        # Skills catalog: agent sees name+description, loads body on demand
        skills        = [str(BASE_DIR / "skills")],
    )
    return agent

# ── Convenience wrapper for FastAPI / dashboard chat endpoint ──────────────────
async def chat(brand_id: str, message: str, history: list[dict] | None = None) -> str:
    """
    Single-turn or multi-turn conversational query.
    Args:
        brand_id: Injected into DB tool calls automatically via closure (future).
                  For now, brand_id is passed in each tool call by the LLM.
        message:  Founder's message.
        history:  Prior conversation turns [{"role": "user/assistant", "content": "..."}]
    Returns:
        Supervisor's response string.
    Usage in FastAPI:
        @router.post("/api/v1/chat")
        async def chat_endpoint(req: ChatRequest, brand: Brand = Depends(get_current_brand)):
            response = await chat(brand.brand_id, req.message, req.history)
            return {"response": response}
    """
    agent = await build_supervisor()
    turns = list(history or [])
    turns.append({"role": "user", "content": message})
    result   = agent.invoke({"messages": turns})
    messages = result.get("messages", [])
    if messages:
        last = messages[-1]
        return getattr(last, "content", str(last))
    return "No response."

# ── Interactive CLI for local testing ─────────────────────────────────────────
async def main():
    print("\n" + "═" * 60)
    print("  FashionOS Supervisor — Conversational Mode")
    print("  Type 'quit' to exit")
    print("═" * 60 + "\n")
    agent   = await build_supervisor()
    history = []
    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("quit", "exit", "q"):
            break
        if not user_input:
            continue
        history.append({"role": "user", "content": user_input})
        result   = agent.invoke({"messages": history})
        messages = result.get("messages", [])
        if messages:
            last    = messages[-1]
            content = getattr(last, "content", str(last))
            print(f"\nFashionOS: {content}\n")
            history.append({"role": "assistant", "content": content})

if __name__ == "__main__":
    asyncio.run(main())