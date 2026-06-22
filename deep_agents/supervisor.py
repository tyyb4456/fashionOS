import os
import asyncio
from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_mcp_adapters.client import MultiServerMCPClient

from subagents import build_inventory_subagent

SHOPIFY_MCP_URL = os.getenv("SHOPIFY_MCP_URL", "http://localhost:8001/mcp")

# ── root = the deep_agents/ folder (where supervisor.py lives) ──────────────
BASE_DIR = Path(__file__).parent  # deep_agents/

SUPERVISOR_PROMPT = """
You are FashionOS Supervisor — the main orchestrator of a Pakistani Shopify fashion brand.
Delegate inventory analysis to inventory-agent.
Synthesize its structured report into clear founder-facing decisions with 🔴🟡🟢 priorities.
Never call Shopify APIs yourself — that's the subagents' job.
"""


async def main():
    client = MultiServerMCPClient(
        {
            "shopify": {
                "url": SHOPIFY_MCP_URL,
                "transport": "streamable_http",
            }
        }
    )
    tools = await client.get_tools()

    # ── build subagents ──────────────────────────────────────────────────────
    inventory_subagent = await build_inventory_subagent(tools)
    # trend_subagent    = await build_trend_subagent(tools)
    # pricing_subagent  = await build_pricing_subagent(tools)

    subagents = [
        inventory_subagent,
        # trend_subagent,
        # pricing_subagent,
    ]

    # ── backend points to deep_agents/ as root ───────────────────────────────
    backend = FilesystemBackend(root_dir=str(BASE_DIR))

    agent = create_deep_agent(
        model="google_genai:gemini-3.5-flash",
        system_prompt=SUPERVISOR_PROMPT,
        backend=backend,
        memory=[str(BASE_DIR / "AGENTS.md")],
        skills=[str(BASE_DIR / "skills/")],   # deep_agents/skills/
        subagents=subagents,
        name="fashionos-supervisor",
    )

    result = await agent.ainvoke({
        "messages": "Run the daily inventory check. What needs action today?"
    })

    print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())