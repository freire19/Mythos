"""Multi-provider canary (Plano-Upgrade-v3 §2.4).

Drives `run_agent` against the SAME fixture for each of the 5 supported
providers and asserts the emitted event stream is equivalent. This is
the regression net for loop-level adapter divergence — bugs where the
agent loop treats output from one provider differently from another
even though the fixture stream is byte-identical.

Wire-format adapter bugs (Anthropic SSE event parsing, OpenAI tool-call
accumulation, etc.) are caught by their dedicated unit tests
(test_llm.py, test_llm_anthropic.py). This canary catches the gap
between those — code paths in agent.py that branch on `provider` and
could subtly diverge.

The fixture used is `tool_then_finish.json`: model emits one tool call,
sees the result, then produces a final text answer. It exercises:
- final-event dispatch
- tool execution + result message reconstruction
- second-turn fetch + done event emission
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alpha import agent as agent_mod
from alpha.llm_fixtures import build_replay_stream
from alpha.tools import ToolCategory, ToolDefinition, ToolSafety


PROVIDERS = ("deepseek", "openai", "anthropic", "grok", "ollama")

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "llm" / "openai" / "tool_then_finish.json"


async def _fake_read_file(path: str) -> dict:
    """Deterministic stand-in for the real read_file tool.

    Whatever path the fixture asked for, return the same content so the
    second turn's input is identical across all 5 provider runs."""
    return {"path": path, "content": "stub content for canary"}


def _fake_get_tool(name: str) -> ToolDefinition | None:
    if name == "read_file":
        return ToolDefinition(
            name="read_file",
            description="canary fake",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            safety=ToolSafety.SAFE,
            executor=_fake_read_file,
            category=ToolCategory.FILESYSTEM,
        )
    return None


def _tool_schema() -> list[dict]:
    """OpenAI-format tool list matching the fake_get_tool above."""
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "canary fake",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]


def _approve_all(_name: str, _args: dict) -> bool:
    """Sync approval callback that always approves. SAFE tools skip the
    approval gate, but wire it up anyway in case the loop probes it."""
    return True


def _normalize(events: list[dict]) -> dict:
    """Reduce the raw event stream to a comparable shape.

    Drop per-provider noise: cost numbers, token counts, model name —
    keep only the structural facts we want all 5 providers to agree on."""
    return {
        "done_reply": next(
            (e.get("reply") for e in events if e.get("type") == "done"), None
        ),
        "tool_call_names": tuple(
            e.get("name") for e in events if e.get("type") == "tool_call"
        ),
        "tool_result_count": sum(
            1 for e in events if e.get("type") == "tool_result"
        ),
        "errors": tuple(
            e.get("message") for e in events if e.get("type") == "error"
        ),
        "token_event_count": sum(
            1 for e in events if e.get("type") == "token"
        ),
    }


async def _run_one(provider: str, monkeypatch) -> dict:
    fake_stream, _state = build_replay_stream(FIXTURE_PATH)
    monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

    messages = [
        {"role": "system", "content": "You are a test agent."},
        {"role": "user", "content": "Please read README.md."},
    ]
    events: list[dict] = []
    async for event in agent_mod.run_agent(
        messages=messages,
        user_message="Please read README.md.",
        temperature=0.5,
        provider=provider,
        get_tool_fn=_fake_get_tool,
        tools=_tool_schema(),
        approval_callback=_approve_all,
    ):
        events.append(event)
        if event.get("type") == "done":
            break  # don't run past the canonical end-of-turn
    return _normalize(events)


@pytest.mark.asyncio
async def test_canary_all_providers_emit_equivalent_event_stream(
    monkeypatch,
):
    """Same fixture, same agent loop, every provider → equivalent output.

    A failure here points at code in alpha/agent/ that reacts to `provider`
    in a way the other providers don't. The shared fixture removes any
    real wire-format variation, so divergence has to come from agent code."""
    # All 5 providers route through cost.py / config.py which expect at
    # least a plausible env var to exist. Real values aren't needed
    # because the LLM call is intercepted by the fixture.
    for env in (
        "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "GROK_API_KEY", "XAI_API_KEY",
    ):
        monkeypatch.setenv(env, "test-canary")
    monkeypatch.setenv("ALPHA_NO_PROJECT_CONTEXT", "1")

    results = {p: await _run_one(p, monkeypatch) for p in PROVIDERS}
    baseline_provider = PROVIDERS[0]
    baseline = results[baseline_provider]

    # Sanity check the baseline isn't accidentally empty — would make every
    # other provider trivially "match" and hide real divergence.
    assert baseline["done_reply"], (
        f"baseline ({baseline_provider}) produced no done event: {baseline}"
    )
    assert baseline["tool_call_names"] == ("read_file",), (
        f"baseline tool_call sequence unexpected: {baseline['tool_call_names']}"
    )

    for provider, result in results.items():
        if provider == baseline_provider:
            continue
        assert result == baseline, (
            f"{provider} diverged from {baseline_provider}:\n"
            f"  baseline: {baseline}\n"
            f"  {provider}: {result}"
        )


@pytest.mark.asyncio
async def test_canary_all_providers_emit_done_event(monkeypatch):
    """Weaker sibling check: every provider at least produces a done event.

    Useful as a separate test because if the heavier equivalence check
    breaks, this one isolates "agent never finished for provider X"
    (loop hang / error) from "agent finished but with different events"
    (real divergence)."""
    for env in (
        "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "GROK_API_KEY", "XAI_API_KEY",
    ):
        monkeypatch.setenv(env, "test-canary")
    monkeypatch.setenv("ALPHA_NO_PROJECT_CONTEXT", "1")

    for provider in PROVIDERS:
        result = await _run_one(provider, monkeypatch)
        assert result["done_reply"], (
            f"{provider} never produced a done event: {result}"
        )
        assert not result["errors"], (
            f"{provider} surfaced errors during the canary run: {result['errors']}"
        )
