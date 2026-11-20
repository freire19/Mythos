"""_detect_error_streak should fire on real tool failures, not on successful
results whose content merely mentions error-related words.

Regression tests for #D104. Two failure modes the detector must get right:

1. False positive (the original bug): the old heuristic matched substrings
   ("error", "exception", "traceback", "permission") anywhere in the result
   text, so reading N files about error handling — like auditing this very
   codebase — tripped a spurious streak and cut the agent off.

2. False negative (the over-correction the code review caught): a follow-up
   keyed off a `ok=false` marker in the string preview, which MISSED the
   detector's primary case — a streak of failing shell commands, which
   `execute_shell` returns as a success-shaped `{exit_code, stdout, stderr}`
   dict carrying no `ok` field (the tool ran fine; the command failed).

The fix classifies from the structured result dict (`_result_is_failure`).
"""
from __future__ import annotations

from alpha.agent.loop import (
    _detect_error_streak,
    _detect_loop,
    _result_is_failure,
)


# ── _result_is_failure: structured classification ──

def test_ok_false_is_failure():
    assert _result_is_failure({"ok": False, "category": "runtime", "error": "boom"})


def test_error_key_is_failure():
    # DL033: a tool self-reporting {error: ...} (before executor annotation).
    assert _result_is_failure({"error": "Redirect bloqueado", "blocked": True})


def test_nonzero_exit_code_is_failure():
    # execute_shell success-shaped dict for a FAILED command — no ok field.
    assert _result_is_failure({"exit_code": 1, "stdout": "", "stderr": "make: *** Error 2"})


def test_negative_exit_code_is_failure():
    # Signal termination (e.g. -9) is still a failed command.
    assert _result_is_failure({"exit_code": -9, "stdout": "", "stderr": ""})


def test_successful_command_is_not_failure():
    assert not _result_is_failure({"exit_code": 0, "stdout": "ok", "stderr": ""})


def test_successful_read_mentioning_errors_is_not_failure():
    # A successful file read whose content discusses errors/exceptions and
    # even contains the literal text "ok=false" must NOT be a failure.
    assert not _result_is_failure({
        "ok": True,
        "content": "raise Exception('error')  # traceback; ok=false; permission denied",
    })


def test_ok_true_with_exit_zero_is_not_failure():
    assert not _result_is_failure({"ok": True, "exit_code": 0})


def test_non_dict_result_is_not_failure():
    assert not _result_is_failure("some string result")
    assert not _result_is_failure(None)


def test_bool_is_not_a_nonzero_exit_code():
    # isinstance(True, int) is True in Python — guard against a stray bool
    # field named exit_code being read as a non-zero code.
    assert not _result_is_failure({"exit_code": True})


# ── _detect_error_streak: counts trailing failures ──

def test_streak_counts_only_consecutive_trailing_failures():
    # success, then 3 failures at the tail
    assert _detect_error_streak([False, True, True, True]) == 3


def test_streak_breaks_on_success():
    # a success in the middle breaks the trailing streak
    assert _detect_error_streak([True, False, True]) == 1


def test_no_streak_when_all_successful():
    assert _detect_error_streak([False, False, False, False]) == 0


# ── _detect_loop integration: failing-command streak is detected ──

def test_failing_command_streak_triggers_loop():
    # The #D104 regression: four consecutive failing shell commands (different
    # commands each time, so the exact/similar/cycle detectors don't fire) must
    # be caught by the error-streak detector.
    call_sigs = ["execute_shell:make"]
    recent_calls = [
        "execute_shell:make a", "execute_shell:make b",
        "execute_shell:make c", "execute_shell:make d",
    ]
    recent_results = ["exit_code=2 stderr=err"] * 4
    recent_failures = [True, True, True, True]
    reason = _detect_loop(call_sigs, recent_calls, recent_results, recent_failures)
    assert reason is not None
    assert "error streak" in reason


def test_successful_reads_do_not_trigger_loop():
    # Four successful reads of error-handling code: no streak.
    call_sigs = ["read_file:a"]
    recent_calls = ["read_file:a", "read_file:b", "read_file:c", "read_file:d"]
    recent_results = ["ok=True content=raise Exception"] * 4
    recent_failures = [False, False, False, False]
    assert _detect_loop(call_sigs, recent_calls, recent_results, recent_failures) is None


def test_detect_loop_without_failures_skips_error_streak():
    # Back-compat: omitting recent_failures must not crash and must skip the
    # error-streak detector (the other four still run).
    call_sigs = ["read_file:a"]
    recent_calls = ["read_file:a"]
    recent_results = ["ok=False error=boom"]
    assert _detect_loop(call_sigs, recent_calls, recent_results) is None
