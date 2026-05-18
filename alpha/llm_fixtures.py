"""
LLM fixture record/replay (Plano-Upgrade-v3 H1 #6 / §2.4.1).

Closes the "integration tests need a real LLM" gap. Fixtures are JSON
files capturing the event stream that `stream_chat_with_tools` would
yield for a given prompt — load one in a test and the agent loop runs
deterministically with no network, no API key, no flakiness.

## Fixture format

```json
{
  "version": 1,
  "scenario": "happy_path",
  "provider": "deepseek",
  "model": "deepseek-chat",
  "turns": [
    [
      {"type": "content_token", "token": "Hello"},
      {"type": "content_token", "token": " world"},
      {"type": "final", "content": "Hello world",
       "tool_calls": [], "reasoning_content": null, "error": null,
       "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    ],
    [
      {"type": "final", "content": "Done.",
       "tool_calls": [], "reasoning_content": null, "error": null}
    ]
  ]
}
```

Each turn is a complete sequence of events from one call to
`stream_chat_with_tools`. The replay generator hands them out
sequentially — first invocation gets turn[0], second gets turn[1], etc.

## Usage in tests

```python
from alpha.llm_fixtures import build_replay_stream

fake_stream, state = build_replay_stream("tests/fixtures/llm/deepseek/happy.json")
monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)
```

## Recording

`record_wrap(source, sink_path)` returns an async generator that yields
the same events as `source` while accumulating them to `sink_path`. To
capture a real session, wrap `stream_chat_with_tools` in calling code
and run against a live provider once.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncGenerator, Callable

logger = logging.getLogger(__name__)


FIXTURE_VERSION = 1


class FixtureError(ValueError):
    """Raised when a fixture file is malformed."""


def load_fixture(path: str | Path) -> dict:
    """Load + validate a fixture JSON file. Returns the parsed dict."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise FixtureError(f"failed to read fixture {p}: {e}") from e
    if not isinstance(data, dict):
        raise FixtureError(f"fixture {p} is not a JSON object")
    if data.get("version") != FIXTURE_VERSION:
        raise FixtureError(
            f"fixture {p} version={data.get('version')!r}, expected {FIXTURE_VERSION}"
        )
    turns = data.get("turns")
    if not isinstance(turns, list) or not turns:
        raise FixtureError(f"fixture {p} has empty or missing 'turns'")
    for i, turn in enumerate(turns):
        if not isinstance(turn, list) or not turn:
            raise FixtureError(f"fixture {p} turn[{i}] is empty or not a list")
        if not any(e.get("type") == "final" for e in turn if isinstance(e, dict)):
            raise FixtureError(f"fixture {p} turn[{i}] has no 'final' event")
    return data


def build_replay_stream(
    fixture_path: str | Path,
    *,
    delay_s: float = 0.0,
) -> tuple[Callable, dict]:
    """Build a `stream_chat_with_tools`-compatible fake from a fixture.

    Returns (fake_stream, state). `state["turn_index"]` is incremented on
    each call so tests can assert how far the agent got. `state["calls"]`
    records each call's (provider, message_count, tool_count) tuple.

    `delay_s` inserts an awaitable sleep between yielded events — useful
    to simulate slow streams or to give the test runner a chance to
    process events as they arrive.
    """
    data = load_fixture(fixture_path)
    turns = data["turns"]
    state: dict = {"turn_index": 0, "calls": [], "exhausted": False}

    async def fake_stream(
        messages: list,
        tools: list,
        temperature: float = 0.5,
        provider: str = "deepseek",
    ) -> AsyncGenerator[dict, None]:
        idx = state["turn_index"]
        state["calls"].append(
            {"provider": provider, "n_messages": len(messages), "n_tools": len(tools or [])}
        )
        if idx >= len(turns):
            state["exhausted"] = True
            raise AssertionError(
                f"fixture {fixture_path} exhausted: agent asked for turn {idx + 1} "
                f"but fixture only has {len(turns)} turn(s)"
            )
        state["turn_index"] = idx + 1
        for event in turns[idx]:
            if delay_s:
                await asyncio.sleep(delay_s)
            yield dict(event)  # copy — callers must not mutate fixture-owned dicts

    return fake_stream, state


def record_wrap(
    source: AsyncGenerator[dict, None],
    sink_path: str | Path,
    *,
    scenario: str = "recorded",
    provider: str = "",
    model: str = "",
) -> AsyncGenerator[dict, None]:
    """Pass events from `source` through unchanged while accumulating
    them to `sink_path` as a one-turn fixture. The sink is always
    written on close (success, crash, or generator cancellation), and
    additionally flushed on the *first* `final` event so a long-running
    stream that crashes after producing a usable turn isn't lost."""

    p = Path(sink_path)
    captured: list[dict] = []
    flushed = False

    async def gen():
        nonlocal flushed
        try:
            async for event in source:
                if isinstance(event, dict):
                    captured.append(_serializable(event))
                yield event
                if (
                    not flushed
                    and isinstance(event, dict)
                    and event.get("type") == "final"
                ):
                    _write_fixture(p, captured, scenario, provider, model)
                    flushed = True
        finally:
            if captured and not flushed:
                _write_fixture(p, captured, scenario, provider, model)

    return gen()


def append_turn(
    path: str | Path,
    events: list[dict],
    *,
    scenario: str = "recorded",
    provider: str = "",
    model: str = "",
) -> None:
    """Append one turn's events to a session fixture (Plano-Upgrade-v3 H2 #9).

    Used by the agent loop's `ALPHA_RECORD_SESSION_PATH` recording path: each
    `stream_chat_with_tools` call buffers its events, then this function
    extends the multi-turn fixture file on `final`. Creates the file if
    missing; otherwise reads, appends to `turns`, and writes atomically.

    For one-shot single-stream captures, use `record_wrap` instead — it
    has a richer API (handles partial-stream sinks via finally) but
    operates on one turn only.
    """
    p = Path(path)
    serialized = [_serializable(e) for e in events if isinstance(e, dict)]
    if not serialized:
        return

    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "turns" not in data:
                data = None
        except (OSError, json.JSONDecodeError):
            data = None
    else:
        data = None

    if data is None:
        data = {
            "version": FIXTURE_VERSION,
            "scenario": scenario,
            "provider": provider,
            "model": model,
            "turns": [],
        }
    data["turns"].append(serialized)

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _serializable(event: dict) -> dict:
    """Drop fields the fixture format can't represent (exception objects, etc.)."""
    out: dict = {}
    for k, v in event.items():
        try:
            json.dumps(v)
            out[k] = v
        except (TypeError, ValueError):
            out[k] = repr(v)
    return out


def _write_fixture(
    path: Path,
    events: list[dict],
    scenario: str,
    provider: str,
    model: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": FIXTURE_VERSION,
        "scenario": scenario,
        "provider": provider,
        "model": model,
        "turns": [events],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info("fixture written: %s (%d events)", path, len(events))
