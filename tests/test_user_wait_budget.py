"""Time spent waiting for the user must not consume the turn budget, and
`/dev/null` is a legal redirect target although it is outside the workspace."""

from __future__ import annotations

import asyncio
import time

import pytest

from alpha import _turn_budget as tb
from alpha import executor
from alpha.tools import pipeline_tools


class TestUserWaitCredit:
    def test_note_user_wait_extends_deadline_and_total(self):
        async def run():
            token = tb.set_deadline(time.monotonic() + 10)
            try:
                before = tb.user_wait_total()
                tb.note_user_wait(3600)
                assert tb.user_wait_total() - before == 3600
                assert tb.remaining() > 3600
            finally:
                tb.reset(token)

        asyncio.run(run())

    def test_no_deadline_and_negative_are_safe(self):
        async def run():
            tb.note_user_wait(-5)
            tb.note_user_wait(5)
            assert tb.remaining() == float("inf")

        asyncio.run(run())

    def test_slow_approval_does_not_expire_the_turn(self):
        """The approval gate credits the answer time back: with 0.3s of
        budget and a 0.6s answer, the tool still has time to start."""

        def slow_approval(name, args):
            time.sleep(0.6)
            return True

        async def run():
            token = tb.set_deadline(time.monotonic() + 0.3)
            try:
                events = [
                    e async for e in executor._gate_one(
                        {"id": "c1"}, "execute_shell", {"command": "x"}, [],
                        lambda n, a: True, slow_approval, lambda n, a: (False, ""),
                    )
                ]
                return events, tb.remaining(), tb.user_wait_total()
            finally:
                tb.reset(token)

        events, left, waited = asyncio.run(run())
        assert [e["type"] for e in events] == ["approval_needed"]
        assert waited >= 0.6
        assert left > 0


class TestDevNullRedirect:
    @pytest.mark.parametrize("cmd", [
        "find /tmp -name x 2>/dev/null | head -20",
        "ls > /dev/null",
        "ls 2> '/dev/null'",
    ])
    def test_dev_null_is_allowed(self, cmd):
        assert pipeline_tools._validate_redirect_paths(cmd) is None

    @pytest.mark.parametrize("cmd", [
        "ls > /etc/passwd",
        "ls > /dev/null/../sda",
        "ls 2>/dev/sda",
    ])
    def test_other_outside_targets_still_blocked(self, cmd):
        assert pipeline_tools._validate_redirect_paths(cmd) is not None

    def test_open_redirect_uses_null_device(self):
        handles = pipeline_tools._open_redirect_files({"stderr": "/dev/null"})
        try:
            assert handles["stderr"].name == "/dev/null"
        finally:
            for h in handles.values():
                h.close()
