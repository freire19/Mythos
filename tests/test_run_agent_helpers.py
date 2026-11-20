"""AUDIT_V2.2 #020: unit tests for the loop-body branches extracted out of the
~455-line run_agent monster — _handle_dsml_drift and _handle_preflight_reprompt.
These were previously inline and untestable in isolation."""

from types import SimpleNamespace


from alpha.agent import _handle_dsml_drift, _handle_preflight_reprompt
from alpha.tools import ToolCategory, ToolSafety


# ─── _handle_dsml_drift ──────────────────────────────────────────────


class TestHandleDsmlDrift:
    def test_non_drift_is_noop(self):
        msgs = []
        action, failures, events = _handle_dsml_drift(
            {"content": "hi", "tool_calls": []}, msgs, 0, "full"
        )
        assert action == "none"
        assert failures == 0
        assert events == []
        assert msgs == []

    def test_first_drift_reprompts_and_appends_correction(self):
        msgs = []
        action, failures, events = _handle_dsml_drift(
            {"dsml_recovery_failed": True, "content": "<invoke name='x'>"},
            msgs, 0, "full",
        )
        assert action == "reprompt"
        assert failures == 1
        assert events == []
        # offending content preserved + corrective user note appended
        assert msgs[0]["role"] == "assistant"
        assert msgs[1]["role"] == "user"
        assert "ALPHA SYSTEM NOTE" in msgs[1]["content"]

    def test_reprompt_without_content_only_appends_note(self):
        msgs = []
        action, failures, _ = _handle_dsml_drift(
            {"dsml_recovery_failed": True}, msgs, 0, "full"
        )
        assert action == "reprompt"
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_second_drift_aborts_with_events(self):
        msgs = []
        action, failures, events = _handle_dsml_drift(
            {"dsml_recovery_failed": True, "content": "x"}, msgs, 1, "the reply"
        )
        assert action == "abort"
        assert failures == 2
        assert [e["type"] for e in events] == ["anomaly", "done"]
        assert events[0]["reason"] == "dsml_unrecoverable"
        assert events[1]["reply"] == "the reply"
        # abort must NOT append a corrective note
        assert msgs == []


# ─── _handle_preflight_reprompt ──────────────────────────────────────


def _fake_get_tool(destructive_names):
    """Return a get_tool_fn where the given names are DESTRUCTIVE non-planning
    tools and everything else is a SAFE tool."""
    def get_tool(name):
        if name in destructive_names:
            return SimpleNamespace(
                category=ToolCategory.FILESYSTEM, safety=ToolSafety.DESTRUCTIVE
            )
        return SimpleNamespace(category=ToolCategory.FILESYSTEM, safety=ToolSafety.SAFE)
    return get_tool


class TestHandlePreflightReprompt:
    def test_fires_when_two_destructive_without_preflight(self):
        msgs = []
        tcs = [{"name": "write_file"}, {"name": "execute_shell"}]
        result = _handle_preflight_reprompt(
            {"tool_calls": tcs, "content": "plan"}, msgs,
            _fake_get_tool({"write_file", "execute_shell"}), 0, 0,
        )
        assert result == 1  # reprompts_this_task + 1
        assert msgs[-1]["role"] == "user"
        assert "pre_flight" in msgs[-1]["content"]

    def test_noop_when_preflight_already_called(self):
        tcs = [{"name": "pre_flight"}, {"name": "write_file"}, {"name": "execute_shell"}]
        result = _handle_preflight_reprompt(
            {"tool_calls": tcs}, [],
            _fake_get_tool({"write_file", "execute_shell"}), 0, 0,
        )
        assert result is None

    def test_noop_below_destructive_threshold(self):
        tcs = [{"name": "write_file"}, {"name": "read_file"}]
        result = _handle_preflight_reprompt(
            {"tool_calls": tcs}, [], _fake_get_tool({"write_file"}), 0, 0
        )
        assert result is None

    def test_noop_when_reprompt_limit_reached(self):
        tcs = [{"name": "write_file"}, {"name": "execute_shell"}]
        result = _handle_preflight_reprompt(
            {"tool_calls": tcs}, [],
            _fake_get_tool({"write_file", "execute_shell"}), 1, 0,  # limit is 1
        )
        assert result is None

    def test_noop_when_no_get_tool_fn(self):
        result = _handle_preflight_reprompt(
            {"tool_calls": [{"name": "x"}]}, [], None, 0, 0
        )
        assert result is None
