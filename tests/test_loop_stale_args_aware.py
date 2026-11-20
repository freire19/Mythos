"""Stale-progress detector should consider args, not just result text.

Regression test for the "reading N distinct audit files looks stale because
all markdown files share an identical header template" false-positive.

The fix: `_detect_stale_progress` now accepts the matching `recent_calls`
window and suppresses pairs where the calls target clearly distinct args
(different file paths after stripping shared prefix).
"""
from __future__ import annotations

from alpha.agent.loop import (
    _args_distinct,
    _detect_loop,
    _detect_stale_progress,
)


def _audit_preview(name: str) -> str:
    """Mimic a `_result_preview` of an audit markdown file. Shape matches
    real `_result_preview` output: `key=value ` chunks (no literal newlines)
    truncated to 500 chars. First ~30 chars are identical across files (the
    template effect), tail differs by name — exactly what fooled the old
    detector."""
    return (
        f"path=docs/audits/DEEP_{name}.md content=# {name.upper()} "
        f"hardening sprint -- closed=1 open=10 high=0 medium=5 baixo=5 "
        f"footer=generated 2026-05-26 author=audit-pipeline"
    )


def _call_sig(path: str) -> str:
    """Mimic `_call_signature` output for a read_file call."""
    return f'read_file:{{"path": "{path}"}}'


class TestArgsDistinct:
    def test_distinct_files_under_same_dir(self):
        """Different audit files in docs/audits/ should be flagged distinct
        after stripping the shared `docs/audits/DEEP_` prefix."""
        a = _call_sig("docs/audits/DEEP_BUGS.md")
        b = _call_sig("docs/audits/DEEP_LOGIC.md")
        assert _args_distinct(a, b) is True

    def test_identical_calls_not_distinct(self):
        a = _call_sig("docs/audits/DEEP_BUGS.md")
        assert _args_distinct(a, a) is False

    def test_different_tools_not_distinct(self):
        """Cross-tool comparison returns False so stale-detection across
        a `read_file` → `grep` flip isn't suppressed."""
        a = 'read_file:{"path": "foo.md"}'
        b = 'grep:{"pattern": "TODO"}'
        assert _args_distinct(a, b) is False


class TestStaleProgressSuppressedByDistinctArgs:
    def test_reading_n_audit_files_does_not_trip(self):
        """The actual case the user hit: 7 distinct audit files, similar
        previews, should NOT be flagged as stale once args are considered."""
        names = ["BUGS", "LOGIC", "RESILIENCE", "SECURITY", "MAINTAINABILITY", "PERFORMANCE"]
        results = [_audit_preview(n) for n in names]
        calls = [_call_sig(f"docs/audits/DEEP_{n}.md") for n in names]
        # Without recent_calls: old behavior — likely flags as stale
        # With recent_calls: should NOT flag, because args are distinct
        assert _detect_stale_progress(results, calls) is False

    def test_genuine_stale_still_caught(self):
        """If args are the same (true repeat) and previews are similar,
        stale detection should still fire."""
        stuck_call = _call_sig("docs/audits/DEEP_BUGS.md")
        stuck_result = _audit_preview("BUGS")
        calls = [stuck_call] * 6
        results = [stuck_result] * 6
        assert _detect_stale_progress(results, calls) is True

    def test_backwards_compat_no_calls_arg(self):
        """Without recent_calls, falls back to original result-only behavior:
        similar-but-not-identical previews (the template-effect case that
        motivated the args-aware fix) STILL trip stale detection in the old
        mode. The rescue only kicks in when calls are passed.

        Uses same-length distinct names so the strings differ by only ~2
        chars total — guaranteed to pass `_quick_similar` via the
        SequenceMatcher path (ratio > 0.90), not via the byte-equality
        fast-path. Catches a regression where the default `recent_calls=None`
        accidentally enabled rescue logic (which would silently make this
        return False)."""
        names = ["AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF"]
        results = [_audit_preview(n) for n in names]
        assert _detect_stale_progress(results) is True

    def test_detect_loop_integration(self):
        """End-to-end through `_detect_loop`: 6 distinct audit reads must
        not return any loop reason."""
        names = ["BUGS", "LOGIC", "RESILIENCE", "SECURITY", "MAINTAINABILITY", "PERFORMANCE"]
        call_sigs = [_call_sig(f"docs/audits/DEEP_{n}.md") for n in names]
        results = [_audit_preview(n) for n in names]
        reason = _detect_loop(call_sigs, call_sigs, results)
        assert reason is None, f"unexpected loop reason: {reason}"

    def test_calls_longer_than_results_aligns_by_tail(self):
        """AUDIT_V2.3 #203: the agent extends `_recent_calls` BEFORE tools
        execute (so before results are appended), then calls
        `_detect_loop`. At that moment the tail of `recent_calls` holds the
        CURRENT batch, which has no matching results yet. The old slice
        `recent_calls[-len(results):]` KEPT that pending tail and dropped
        the oldest executed calls — every pair compared off-by-batch. The
        caller now passes `pending=<batch size>` so the pending tail is
        dropped before aligning.

        Construct: 6 distinct audit reads (results) + 8 calls where the
        LAST 2 are the pending batch. With `pending=2`, the function
        aligns exactly the 6 real calls to the 6 results, sees distinct
        paths, and rescues correctly."""
        names = ["BUGS", "LOGIC", "RESILIENCE", "SECURITY", "MAINTAINABILITY", "PERFORMANCE"]
        results = [_audit_preview(n) for n in names]
        # The 6 calls that ACTUALLY produced the 6 results:
        real_calls = [_call_sig(f"docs/audits/DEEP_{n}.md") for n in names]
        # Simulate eager extension: the next batch is already in the list,
        # but its results have NOT been appended yet:
        pending_batch = [_call_sig("docs/audits/DEEP_BUGS.md")] * 2
        all_calls = real_calls + pending_batch
        assert _detect_stale_progress(results, all_calls, pending=2) is False

    def test_pending_batch_does_not_suppress_genuine_stale(self):
        """#203 discriminating case: genuinely stale (6 identical calls →
        6 identical results), and the PENDING batch happens to target
        distinct files. The old tail-slice aligned the pending calls to
        executed results and the args-distinct rescue suppressed a REAL
        stale loop. With `pending=2` the pending tail is excluded and the
        stale loop is caught."""
        stuck = _call_sig("docs/audits/DEEP_BUGS.md")
        results = [_audit_preview("BUGS")] * 6
        pending_batch = [
            _call_sig("alpha/agent/loop.py"),
            _call_sig("alpha/executor.py"),
        ]
        calls = [stuck] * 6 + pending_batch
        # Old behavior (no pending): rescued by the misaligned pending tail.
        assert _detect_stale_progress(results, calls) is False
        # Correct caller contract: pending informed → stale detected.
        assert _detect_stale_progress(results, calls, pending=2) is True
