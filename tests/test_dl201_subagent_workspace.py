"""#DL201: agent-profile `workspace` confinement must be honored in delegation.

`_resolve_subagent_workspace` picks the sub-agent's boundary: a profile may
only RESTRICT (never widen) the parent boundary, and a confined parent
propagates to its sub-agents.
"""

from __future__ import annotations

import types
from pathlib import Path


def _ns(ws):
    return types.SimpleNamespace(workspace=ws)


def test_profile_workspace_within_parent_is_honored(tmp_path):
    from alpha.tools import delegate_tools as dt

    parent = tmp_path / "parent"
    sub = parent / "sub"
    sub.mkdir(parents=True)
    got = dt._resolve_subagent_workspace(_ns(str(sub)), str(parent))
    assert Path(got).resolve() == sub.resolve()


def test_profile_workspace_outside_parent_is_ignored(tmp_path):
    # A profile may only restrict, not widen → an out-of-bounds workspace falls
    # back to the parent boundary (never escapes it).
    from alpha.tools import delegate_tools as dt

    parent = tmp_path / "parent"
    outside = tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    got = dt._resolve_subagent_workspace(_ns(str(outside)), str(parent))
    assert Path(got).resolve() == parent.resolve()


def test_no_profile_workspace_uses_parent(tmp_path):
    from alpha.tools import delegate_tools as dt

    parent = tmp_path / "parent"
    parent.mkdir()
    assert Path(dt._resolve_subagent_workspace(_ns(None), str(parent))).resolve() == parent.resolve()
    assert Path(dt._resolve_subagent_workspace(None, str(parent))).resolve() == parent.resolve()


def test_confined_parent_propagates_via_active_workspace(tmp_path):
    # A confined parent (active_workspace set, no explicit parent_workspace)
    # propagates its boundary to the sub-agent instead of the global launch dir.
    from alpha.tools import delegate_tools as dt
    from alpha.agents.workspace import active_workspace

    parent = tmp_path / "parent"
    parent.mkdir()
    token = active_workspace.set(str(parent))
    try:
        got = dt._resolve_subagent_workspace(None, None)
        assert Path(got).resolve() == parent.resolve()
    finally:
        active_workspace.reset(token)
