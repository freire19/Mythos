"""Google Antigravity agent tool for ALPHA.

Allows delegating complex web navigation tasks to the Antigravity agent.
"""

from __future__ import annotations

import logging
import os

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool

logger = logging.getLogger(__name__)

async def _google_antigravity(prompt: str, base_agent: str = "antigravity-preview-05-2026") -> dict:
    """Delegate a task to the Google Antigravity agent."""
    try:
        from google import genai
    except ImportError:
        return {
            "error": "The 'google-genai' package is required to use the Antigravity agent. "
                     "Install it with: pip install google-genai"
        }

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"error": "GEMINI_API_KEY environment variable is not set."}

    try:
        import asyncio

        def _run_sync():
            client = genai.Client(api_key=api_key)
            # AUDIT_V2.3 #703: a linha GA do google-genai (>=1.0) usa
            # `agent`/`input` em `interactions.create` — os kwargs antigos
            # (`base_agent`/`prompt`) eram da API que nunca existiu na 0.x
            # que o pin antigo instalava.
            resposta = client.interactions.create(
                agent=base_agent,
                input=prompt,
            )
            text = getattr(resposta, "text", None)
            return text if text is not None else str(resposta)

        loop = asyncio.get_running_loop()
        result_text = await loop.run_in_executor(None, _run_sync)

        return {
            "status": "success",
            "result": result_text
        }
    except Exception as e:
        logger.warning(f"antigravity tool error: {e}")
        return {"error": str(e)}

register_tool(
    ToolDefinition(
        name="google_antigravity",
        description=(
            "Delegate a complex web navigation or scraping task to the Google Antigravity agent. "
            "Use this when you need an agent to autonomously open a browser, navigate, and extract data. "
            "Returns the final summary/result from the agent."
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Clear instructions for the Antigravity agent (e.g., 'Acesse o site X, busque Y, extraia Z').",
                },
                "base_agent": {
                    "type": "string",
                    "description": "The Antigravity model identifier.",
                    "default": "antigravity-preview-05-2026",
                },
            },
            "required": ["prompt"],
        },
        safety=ToolSafety.SAFE,
        executor=_google_antigravity,
        category=ToolCategory.AGENT,
    )
)
