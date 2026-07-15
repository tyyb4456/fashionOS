# deep_agents/message_sanitizer.py
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage


def _flatten_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "image_url":
                    return content  # leave vision blocks alone
                # drop reasoning / tool_use / anything non-text
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


class SanitizeMessagesMiddleware(AgentMiddleware):
    """
    Strips non-text content blocks (e.g. {'type': 'reasoning', ...}) from AI
    messages before they're sent to the model. Needed because
    ModelFallbackMiddleware can hand model2 (Ollama, strict OpenAI-compatible
    endpoint) message history shaped by model1 (Kimi/Azure) — Ollama rejects
    any content block that isn't 'text' or 'image_url'.
    """

    def before_model(self, state, runtime):
        messages = state.get("messages", [])
        changed  = False
        cleaned  = []

        for m in messages:
            if isinstance(m, AIMessage) and not isinstance(m.content, str):
                new_content = _flatten_content(m.content)
                if new_content != m.content:
                    cleaned.append(m.model_copy(update={"content": new_content}))
                    changed = True
                    continue
            cleaned.append(m)

        return {"messages": cleaned} if changed else None