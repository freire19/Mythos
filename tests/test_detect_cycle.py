"""AUDIT_V2.2 #027: functional coverage for the cycle detector.

`_detect_cycle` (one of the 4 loop detectors) had no behavioural test — the
other three did. It detects A→B→A→B style oscillation (e.g. edit_file↔run_tests)
that the exact-repeat detector misses. The logic (3 full repetitions of cycles
of length 2 and 3, exact per-segment compare) is off-by-one-prone."""

from alpha.agent.loop import _detect_cycle


class TestDetectCycle:
    def test_abab_three_reps_fires(self):
        # length-2 cycle repeated 3× (6 calls)
        assert _detect_cycle(["a:{}", "b:{}"] * 3) is True

    def test_abc_three_reps_fires(self):
        # length-3 cycle repeated 3× (9 calls)
        assert _detect_cycle(["a:{}", "b:{}", "c:{}"] * 3) is True

    def test_two_reps_not_enough(self):
        # only 2 repetitions (4 calls) — below the 6-call floor and <3 reps
        assert _detect_cycle(["a:{}", "b:{}"] * 2) is False

    def test_under_six_calls_never_fires(self):
        assert _detect_cycle(["a:{}", "b:{}", "c:{}", "d:{}", "e:{}"]) is False

    def test_distinct_calls_no_cycle(self):
        assert _detect_cycle([f"t{i}:{{}}" for i in range(8)]) is False

    def test_cycle_only_in_older_window_ignored(self):
        # cycle in the past but the recent tail is not cyclic → no fire
        calls = ["a:{}", "b:{}"] * 3 + ["x:{}", "y:{}", "z:{}", "w:{}", "v:{}", "u:{}"]
        assert _detect_cycle(calls) is False
