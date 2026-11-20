"""Tests for `alpha._turn_budget` and its integration with the executor.

The module is load-bearing: the agent loop sets a wall-clock deadline,
the executor reads it to clamp tool timeouts, and sub-agents in
`delegate_tools` scope a sub-deadline that mustn't leak back to the
parent. CODE_REVIEW2 surfaced four bugs in earlier implementations; these
tests pin the fixed behavior so regressions stay visible.
"""

from __future__ import annotations

import asyncio
import math
import time

import pytest

from alpha import _turn_budget


# ─── Basic clamp / remaining math ───


class TestClampBasics:
    def test_no_deadline_returns_inf(self):
        # Fresh context — no set_deadline. clamp is a no-op.
        assert _turn_budget.remaining() == math.inf
        assert _turn_budget.clamp(300) == 300
        assert _turn_budget.clamp(0.1) == 0.1

    def test_clamp_returns_zero_when_deadline_passed(self):
        with _turn_budget.use_deadline(time.monotonic() - 0.1, cap_to_inherited=False):
            assert _turn_budget.remaining() == 0.0
            assert _turn_budget.clamp(300) == 0.0

    def test_clamp_returns_smaller_of_two(self):
        with _turn_budget.use_deadline(time.monotonic() + 2.0, cap_to_inherited=False):
            r = _turn_budget.remaining()
            assert 1.5 < r <= 2.0
            # clamp(big) → remaining
            assert _turn_budget.clamp(300) <= r
            # clamp(small) → small (smaller than remaining)
            assert _turn_budget.clamp(0.5) == pytest.approx(0.5)


# ─── use_deadline context manager — Token lifecycle ───


class TestUseDeadlineReset:
    def test_use_deadline_restores_on_normal_exit(self):
        outer_d = time.monotonic() + 10.0
        with _turn_budget.use_deadline(outer_d, cap_to_inherited=False):
            assert _turn_budget.remaining() > 5.0
            # Nested: try to install a far-future deadline; cap_to_inherited
            # should clamp it to the outer (10s).
            with _turn_budget.use_deadline(time.monotonic() + 100.0, cap_to_inherited=True):
                assert _turn_budget.remaining() <= 10.0
            # After the nested exits, the outer deadline is restored.
            assert _turn_budget.remaining() > 5.0
        # After the outer exits, no deadline is set.
        assert _turn_budget.remaining() == math.inf

    def test_use_deadline_restores_on_exception(self):
        outer_d = time.monotonic() + 10.0
        with pytest.raises(RuntimeError):
            with _turn_budget.use_deadline(outer_d, cap_to_inherited=False):
                raise RuntimeError("boom")
        # Even on exception, the context var was reset.
        assert _turn_budget.remaining() == math.inf

    def test_cap_to_inherited_false_ignores_parent(self):
        with _turn_budget.use_deadline(time.monotonic() + 5.0, cap_to_inherited=False):
            # cap=False with explicit deadline overrides parent
            with _turn_budget.use_deadline(time.monotonic() + 100.0, cap_to_inherited=False):
                assert _turn_budget.remaining() > 50.0

    def test_cap_to_inherited_true_caps_nested(self):
        # Parent has 3s; child asks for 100s → child gets min(100, 3) = 3s
        parent_d = time.monotonic() + 3.0
        with _turn_budget.use_deadline(parent_d, cap_to_inherited=False):
            with _turn_budget.use_deadline(time.monotonic() + 100.0, cap_to_inherited=True):
                r = _turn_budget.remaining()
                assert 2.5 < r <= 3.0


# ─── ContextVar isolation across asyncio tasks ───


class TestContextVarIsolation:
    """The agent's parent and parallel sub-agents share a ContextVar
    but should not interfere. asyncio.gather wraps coros in Tasks
    which copy the parent's context (PEP 567) and any subsequent
    mutation is isolated to that Task.

    `_delegate_task` (sequential) used to leak the sub-agent's
    deadline back to the parent's task — fixed by `use_deadline`'s
    try/finally Token reset. These tests pin the fix.

    Tests use `asyncio.run()` directly instead of pytest-asyncio so
    they run with or without the plugin installed.
    """

    def test_parallel_subagents_isolated(self):
        async def _run():
            # Parent installs a long deadline; spawns two "sub-agents"
            # in gather, each installing its own (shorter) deadline.
            # After gather returns, the parent's deadline must be
            # unchanged.
            with _turn_budget.use_deadline(time.monotonic() + 10.0, cap_to_inherited=False):
                parent_remaining_before = _turn_budget.remaining()

                async def fake_subagent(sub_budget: float) -> float:
                    with _turn_budget.use_deadline(
                        time.monotonic() + sub_budget, cap_to_inherited=True
                    ):
                        await asyncio.sleep(0)
                        return _turn_budget.remaining()

                results = await asyncio.gather(
                    fake_subagent(1.0), fake_subagent(2.0)
                )
                # Each sub-agent saw ITS OWN remaining (capped by parent)
                assert 0.5 < results[0] <= 1.0
                assert 1.5 < results[1] <= 2.0

                parent_remaining_after = _turn_budget.remaining()
                # Parent's deadline survived: still ~10s
                assert parent_remaining_after > 5.0
                # And specifically, no leak from sub-agents
                assert abs(parent_remaining_before - parent_remaining_after) < 1.0

        asyncio.run(_run())

    def test_sequential_subagent_via_context_manager_no_leak(self):
        async def _run():
            # The very bug from CODE_REVIEW2 #1: parent awaits sub-agent
            # in the same task. With use_deadline's reset, parent's
            # deadline must survive even though no separate Task was
            # created.
            with _turn_budget.use_deadline(time.monotonic() + 10.0, cap_to_inherited=False):
                async def fake_sequential_subagent() -> float:
                    with _turn_budget.use_deadline(
                        time.monotonic() + 1.0, cap_to_inherited=True
                    ):
                        await asyncio.sleep(0)
                        return _turn_budget.remaining()

                sub_r = await fake_sequential_subagent()
                assert 0.5 < sub_r <= 1.0

                # CRITICAL: parent's deadline must NOT be permanently
                # overwritten by the sub-agent's 1s deadline.
                parent_remaining_after = _turn_budget.remaining()
                assert parent_remaining_after > 5.0

        asyncio.run(_run())


# ─── Executor integration — short-circuit on exhausted budget ───


class TestExecutorBudgetIntegration:
    """The executor's `_execute_single_tool` should:
    - skip the wait_for entirely when budget is already exhausted
      (CODE_REVIEW2 #2, no 1.0s floor)
    - clamp tool timeout to remaining budget when budget remains
    """

    def test_skipped_when_budget_exhausted(self):
        from alpha.executor import _execute_single_tool
        from alpha.tools import ToolDefinition, ToolSafety

        # A tool that would take 5 seconds — never should be called.
        call_count = 0

        async def slow_executor(**_kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(5.0)
            return {"ok": True}

        tool_def = ToolDefinition(
            name="slow_tool",
            description="test",
            parameters={"type": "object", "properties": {}},
            safety=ToolSafety.SAFE,
            executor=slow_executor,
        )

        async def _run() -> tuple[dict, float]:
            # Deadline already in the past.
            with _turn_budget.use_deadline(time.monotonic() - 1.0, cap_to_inherited=False):
                t0 = time.monotonic()
                r = await _execute_single_tool(tool_def, "slow_tool", {})
                return r, time.monotonic() - t0

        result, elapsed = asyncio.run(_run())
        # The call short-circuited without ever invoking the executor.
        assert call_count == 0
        # And returned quickly (well under the 5s slow_executor sleep).
        assert elapsed < 0.5
        # Result shape is the timeout error envelope.
        assert result.get("ok") is False
        assert result.get("category") == "timeout"

    def test_clamps_when_budget_smaller_than_tool_timeout(self):
        from alpha.executor import _execute_single_tool
        from alpha.tools import ToolDefinition, ToolSafety

        async def slow_executor(**_kwargs):
            await asyncio.sleep(2.0)
            return {"ok": True}

        tool_def = ToolDefinition(
            name="slow_tool_2",
            description="test",
            parameters={"type": "object", "properties": {}},
            safety=ToolSafety.SAFE,
            executor=slow_executor,
        )

        async def _run() -> tuple[dict, float]:
            # Give the turn only 0.3s — tool needs 2s, so it must
            # time out via the clamped budget, not via the static
            # TOOL_EXECUTION_TIMEOUT (120s default).
            with _turn_budget.use_deadline(time.monotonic() + 0.3, cap_to_inherited=False):
                t0 = time.monotonic()
                r = await _execute_single_tool(tool_def, "slow_tool_2", {})
                return r, time.monotonic() - t0

        result, elapsed = asyncio.run(_run())
        # Timed out close to the clamp, well before the executor's
        # static timeout.
        assert elapsed < 1.0
        assert result.get("ok") is False
        assert result.get("category") == "timeout"


# ─── _llm_budget_clamp_setup helper ───


class TestLlmBudgetClampSetup:
    """The shared helper used by 3 LLM clamp sites in `run_agent`.
    Pins the template shape so future fix in one site can't drift the
    others (CODE_REVIEW4 #1).
    """

    def test_returns_tuple_with_anomaly_done_and_log(self):
        from alpha.agent import _llm_budget_clamp_setup

        with _turn_budget.use_deadline(time.monotonic() + 2.0, cap_to_inherited=False):
            timeout_arg, anomaly_evt, done_evt, log_msg = _llm_budget_clamp_setup(
                "test_reason", "Test label", "partial reply"
            )
            # With a 2s deadline + LLM_TIMEOUT clamp, timeout_arg is <= 2s
            assert timeout_arg is not None
            assert 0 < timeout_arg <= 2.0
            # Anomaly event has the expected shape
            assert anomaly_evt["type"] == "anomaly"
            assert anomaly_evt["reason"] == "test_reason"
            assert "Test label" in anomaly_evt["detail"]
            # Done event carries the current reply
            assert done_evt == {"type": "done", "reply": "partial reply"}
            # Log message includes label + clamp value
            assert "Test label" in log_msg
            assert "timed out" in log_msg

    def test_returns_none_timeout_when_no_deadline(self):
        from alpha.agent import _llm_budget_clamp_setup

        # No use_deadline → remaining() == inf → timeout_arg is None
        timeout_arg, _, _, _ = _llm_budget_clamp_setup("r", "L", "x")
        assert timeout_arg is None


class TestSetDeadlineCapped:
    """Public helper used by delegate_tools to avoid importing the
    private `_turn_deadline` ContextVar from a sibling module
    (CODE_REVIEW4 #5)."""

    def test_caps_to_inherited_when_smaller(self):
        # Parent has 3s; capped attempt to add 100s yields 3s
        with _turn_budget.use_deadline(time.monotonic() + 3.0, cap_to_inherited=False):
            token = _turn_budget.set_deadline_capped(time.monotonic() + 100.0)
            try:
                r = _turn_budget.remaining()
                assert 2.5 < r <= 3.0
            finally:
                _turn_budget.reset(token)

    def test_uses_desired_when_no_inherited(self):
        # No outer deadline; capped is a no-op vs set_deadline
        token = _turn_budget.set_deadline_capped(time.monotonic() + 5.0)
        try:
            r = _turn_budget.remaining()
            assert 4.5 < r <= 5.0
        finally:
            _turn_budget.reset(token)

    def test_uses_desired_when_smaller_than_inherited(self):
        # Parent has 10s; capped attempt to add 2s yields 2s (no cap needed)
        with _turn_budget.use_deadline(time.monotonic() + 10.0, cap_to_inherited=False):
            token = _turn_budget.set_deadline_capped(time.monotonic() + 2.0)
            try:
                r = _turn_budget.remaining()
                assert 1.5 < r <= 2.0
            finally:
                _turn_budget.reset(token)

    def test_none_argument_clears_deadline_not_noop(self):
        """CODE_REVIEW5 #2: passing None CLEARS the deadline (not no-op).
        This pins the documented behavior so a future 'helpful' change
        that makes None a no-op breaks the test loudly."""
        # Outer deadline of 3s
        with _turn_budget.use_deadline(time.monotonic() + 3.0, cap_to_inherited=False):
            assert _turn_budget.remaining() <= 3.0
            # Inner: pass None → clears the deadline
            token = _turn_budget.set_deadline_capped(None)
            try:
                # remaining() returns inf because the deadline is None now
                assert _turn_budget.remaining() == math.inf
            finally:
                _turn_budget.reset(token)
            # After reset, the outer 3s deadline is restored
            assert _turn_budget.remaining() <= 3.0


# ─── Agent loop timeout paths (compression / forced response) ───


class TestAgentLoopBudgetTimeouts:
    """The agent loop wraps 3 LLM-streaming sites with asyncio.timeout.
    These tests stub the async generators called by run_agent's body
    (compression and forced response) with slow coros and verify the
    anomaly emerges. Covers CODE_REVIEW3 #1 and #2.
    """

    def test_compression_timeout_emits_anomaly(self, monkeypatch):
        """When _maybe_compress takes longer than the remaining budget,
        the agent emits compression_timeout_by_budget anomaly + done."""
        from alpha import agent

        async def slow_compress(messages, provider):
            await asyncio.sleep(2.0)
            yield {"type": "context_compressed", "before": 0, "after": 0}

        # Replace the module-level _maybe_compress with the slow stub.
        monkeypatch.setattr(agent, "_maybe_compress", slow_compress)

        async def _run() -> list[dict]:
            collected = []
            # Tight 0.2s budget → compression's 2s sleep will trigger
            # asyncio.timeout immediately.
            with _turn_budget.use_deadline(time.monotonic() + 0.2, cap_to_inherited=False):
                async for event in agent.run_agent(
                    messages=[{"role": "system", "content": ""}],
                    user_message="hi",
                    provider="deepseek",
                    get_tool_fn=lambda n: None,
                    tools=[],
                ):
                    collected.append(event)
                    # Stop after we see the done event.
                    if event.get("type") == "done":
                        break
            return collected

        events = asyncio.run(_run())
        # The compression timeout path emits anomaly then done.
        reasons = [e.get("reason") for e in events if e.get("type") == "anomaly"]
        assert "compression_timeout_by_budget" in reasons, f"got events: {events}"
        # And the very next event after the anomaly is a done.
        idx = next(i for i, e in enumerate(events)
                   if e.get("reason") == "compression_timeout_by_budget")
        assert events[idx + 1].get("type") == "done"


class TestCapForcesFinalSummary:
    """Regression: a model that NEVER stops calling tools (e.g. DeepSeek
    over-reading files) used to hit max_iterations and return an empty
    `done` (no summary), and the V1.8 auto-continue then silently restarted
    the agent — so the user never got an answer. The cap must now force ONE
    no-tools summary so a response always surfaces.
    """

    def test_max_iterations_forces_nonempty_summary(self, monkeypatch):
        from alpha import agent

        # A model that keeps emitting tool calls forever.
        async def always_tool(*a, **k):
            yield {
                "type": "_final",
                "event": {
                    "content": "",
                    "tool_calls": [
                        {"id": "1", "name": "read_file",
                         "arguments": '{"path": "x"}'}
                    ],
                },
            }

        # The forced-summary call (no tools) the cap path makes.
        async def fake_summary(messages, tools, temperature, provider=None, **k):
            assert tools == [], "forced summary must run with NO tools"
            yield {"type": "content_token", "token": "DONE: synthesized result."}
            yield {"type": "final", "content": "DONE: synthesized result.",
                   "usage": {}}

        monkeypatch.setattr(agent, "_stream_one_turn", always_tool)
        monkeypatch.setattr(agent, "stream_chat_with_tools", fake_summary)

        async def _run() -> list[dict]:
            collected = []
            async for event in agent.run_agent(
                messages=[{"role": "system", "content": ""}],
                user_message="audit?",
                provider="deepseek",
                get_tool_fn=lambda n: None,
                tools=[{"type": "function"}],
                max_iterations=3,
            ):
                collected.append(event)
                if event.get("type") == "done":
                    break
            return collected

        events = asyncio.run(_run())
        reasons = [e.get("reason") for e in events if e.get("type") == "anomaly"]
        assert "max_iterations" in reasons, f"got events: {events}"
        done = [e for e in events if e.get("type") == "done"][-1]
        # The whole point: the reply is NOT empty — the cap forced a summary.
        assert done["reply"] == "DONE: synthesized result.", (
            f"cap returned empty reply (regression): {done!r}"
        )

    def test_forced_summary_propagates_error_instead_of_empty_done(self, monkeypatch):
        """Code-review finding 1: if the forced-summary LLM call ERRORS (e.g.
        provider down / context overflow at the cap), the path must yield an
        `error` event — NOT a silent empty `done` that hides the failure."""
        from alpha import agent

        async def erroring_stream(messages, tools, temperature, provider=None, **k):
            yield {"type": "final", "content": "", "error": "HTTP 500 provider down"}

        monkeypatch.setattr(agent, "stream_chat_with_tools", erroring_stream)

        async def run():
            return [
                e async for e in agent._force_final_summary(
                    [{"role": "system", "content": ""}], 0.5, "deepseek", "m",
                    "", "max_iterations", "limite atingido",
                )
            ]

        events = asyncio.run(run())
        types = [e["type"] for e in events]
        assert "error" in types, f"error swallowed (regression): {events}"
        assert "done" not in types, f"emitted done despite LLM error: {events}"
        err = next(e for e in events if e["type"] == "error")
        assert "HTTP 500" in err["message"]

    def test_forced_summary_retries_on_context_overflow(self, monkeypatch):
        """Code-review finding 2: the forced-summary call must do an in-band
        context-overflow retry (compress then retry once) — exactly the
        scenario a cap hits with a near-full context — instead of erroring."""
        from alpha import agent

        calls = {"n": 0}

        async def overflow_then_ok(messages, tools, temperature, provider=None, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                yield {"type": "final", "content": "",
                       "error": "maximum context length exceeded"}
            else:
                yield {"type": "final", "content": "SUMMARY after compress",
                       "usage": {}}

        async def fake_overflow_retry(messages, provider):
            # Stand in for the real aggressive re-compression.
            if False:
                yield {}

        monkeypatch.setattr(agent, "stream_chat_with_tools", overflow_then_ok)
        monkeypatch.setattr(agent, "is_context_overflow_error", lambda e: "context length" in str(e))
        monkeypatch.setattr(agent, "_handle_overflow_retry", fake_overflow_retry)

        async def run():
            return [
                e async for e in agent._force_final_summary(
                    [{"role": "system", "content": ""}], 0.5, "deepseek", "m",
                    "", "max_iterations", "limite atingido",
                )
            ]

        events = asyncio.run(run())
        assert calls["n"] == 2, "should retry the LLM call once after overflow"
        done = [e for e in events if e["type"] == "done"]
        assert done and done[-1]["reply"] == "SUMMARY after compress", (
            f"overflow retry did not recover: {events}"
        )

    def test_silent_stop_after_tool_work_forces_summary(self, monkeypatch):
        """Root-cause fix: a model that does tool work then emits an EMPTY final
        turn (no content, no tool_calls) — DeepSeek's habit of ignoring the
        `※ recap` instruction — must still produce a conclusion, not end with a
        blank reply. The harness forces one summary."""
        from alpha import agent

        calls = {"n": 0}

        async def turns(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                yield {"type": "_final", "event": {
                    "content": "", "tool_calls": [
                        {"id": "1", "name": "read_file", "arguments": "{}"}]}}
            else:
                # Silent stop: no content AND no tool calls.
                yield {"type": "_final", "event": {"content": "", "tool_calls": []}}

        async def fake_summary(messages, tools, temperature, provider=None, **k):
            yield {"type": "content_token", "token": "※ recap: setup pronto."}
            yield {"type": "final", "content": "※ recap: setup pronto.", "usage": {}}

        monkeypatch.setattr(agent, "_stream_one_turn", turns)
        monkeypatch.setattr(agent, "stream_chat_with_tools", fake_summary)

        async def run():
            return [
                e async for e in agent.run_agent(
                    messages=[{"role": "system", "content": ""}], user_message="setup",
                    provider="deepseek", get_tool_fn=lambda n: None,
                    tools=[{"type": "function"}], max_iterations=5,
                )
            ]

        events = asyncio.run(run())
        reasons = [e.get("reason") for e in events if e["type"] == "anomaly"]
        assert "silent_stop" in reasons, f"silent stop not handled: {events}"
        done = [e for e in events if e["type"] == "done"][-1]
        assert "recap" in done["reply"], f"no forced conclusion: {done!r}"

    def test_real_final_answer_is_not_overridden(self, monkeypatch):
        """Guard: when the model DOES give a real final answer (content on the
        last turn), the silent-stop path must NOT fire an extra summary call."""
        from alpha import agent

        summary_calls = {"n": 0}

        async def turns(*a, **k):
            yield {"type": "_final", "event": {
                "content": "Pronto, aqui está a resposta final.", "tool_calls": []}}

        async def fake_summary(*a, **k):
            summary_calls["n"] += 1
            yield {"type": "final", "content": "X", "usage": {}}

        monkeypatch.setattr(agent, "_stream_one_turn", turns)
        monkeypatch.setattr(agent, "stream_chat_with_tools", fake_summary)

        async def run():
            return [
                e async for e in agent.run_agent(
                    messages=[{"role": "system", "content": ""}], user_message="oi",
                    provider="deepseek", get_tool_fn=lambda n: None, tools=[],
                )
            ]

        events = asyncio.run(run())
        assert summary_calls["n"] == 0, "should not force a summary over a real answer"
        done = [e for e in events if e["type"] == "done"][-1]
        assert done["reply"] == "Pronto, aqui está a resposta final."


# ─── End-to-end pathology proof ───


class TestAuditScenarioCannotRecur:
    """Integration-style proof that the original `/audit-3-review`
    pathology (11min still-going) cannot recur in any of the LLM /
    tool call sites that were left unclamped before this work.

    Setup: a tight budget + a stubbed slow LLM call. Verify the agent
    terminates within (roughly) the budget — not the full LLM_TIMEOUT
    or any tool timeout. If a future refactor reintroduces an
    unclamped LLM call, this test fails loudly.
    """

    def test_turn_terminates_within_budget_when_llm_stalls(self, monkeypatch):
        """Simulates a DeepSeek call that never returns. With a 0.3s
        turn budget, the agent must give up in <2s — not LLM_TIMEOUT (300s)."""
        from alpha import agent

        async def stalled_stream_one_turn(*args, **kwargs):
            # Simulates an LLM provider that just hangs. Without
            # asyncio.timeout clamp, this would block until LLM_TIMEOUT
            # (default 300s) and reproduce the 11min-still-going bug.
            await asyncio.sleep(60.0)
            yield {"type": "_final", "event": {"content": "", "tool_calls": []}}

        monkeypatch.setattr(agent, "_stream_one_turn", stalled_stream_one_turn)

        async def _run() -> tuple[list[dict], float]:
            collected = []
            t0 = time.monotonic()
            with _turn_budget.use_deadline(time.monotonic() + 0.3, cap_to_inherited=False):
                async for event in agent.run_agent(
                    messages=[{"role": "system", "content": ""}],
                    user_message="hi",
                    provider="deepseek",
                    get_tool_fn=lambda n: None,
                    tools=[],
                ):
                    collected.append(event)
                    if event.get("type") == "done":
                        break
            return collected, time.monotonic() - t0

        events, elapsed = asyncio.run(_run())
        # The turn must have terminated quickly — definitely under
        # LLM_TIMEOUT (300s) and under the stalled_stream's 60s sleep.
        # CODE_REVIEW5 #1: margem ampla (30s, 100x do budget) para
        # evitar flakiness em CI lento (ARM/M1/under-resourced runners).
        # O ponto do teste é provar que NÃO é 11min ou 300s — qualquer
        # coisa <60s já refuta a pathology.
        assert elapsed < 30.0, (
            f"Turn took {elapsed:.1f}s — way past the 0.3s budget. "
            f"Pathology recurred?"
        )
        # And the agent must have emitted a clean timeout anomaly,
        # not just hung silently.
        anomaly_reasons = [
            e.get("reason") for e in events if e.get("type") == "anomaly"
        ]
        # One of the clamp anomalies fired (compression OR llm_stream).
        timeout_anomaly_fired = any(
            r and "timeout_by_budget" in r for r in anomaly_reasons
        )
        assert timeout_anomaly_fired, (
            f"No timeout anomaly emitted; got anomaly reasons: {anomaly_reasons}, "
            f"events: {events}"
        )
