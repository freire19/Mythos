"""Tests for `alpha.agents.workspace.validate_args` — workspace boundary enforcement.

DEEP_SECURITY V3.3 #035 fix: `validate_workspace_args` é chamada em toda
tool execution e tinha cobertura de 17% pre-V3.3. Esta suite cobre todos
os branches: path absoluto fora, path relativo, traversal `../`, symlinks
apontando fora, paths default-to-workspace, tools sem path params.
"""

from pathlib import Path

import pytest

from alpha.agents.workspace import (
    DEFAULT_TO_WORKSPACE,
    PATH_PARAMS_BY_TOOL,
    validate_args,
)


# ─── Tools sem path params ──────────────────────────────────────────


class TestUnmappedTool:
    """Tools fora de PATH_PARAMS_BY_TOOL passam args inalterados."""

    def test_tool_not_in_map_returns_ok_unchanged(self, tmp_path: Path):
        args = {"random": "value", "x": 1}
        ok, new_args, err = validate_args(str(tmp_path), "totally_unknown_tool", args)
        assert ok is True
        assert new_args == args
        assert err == ""

    def test_known_tool_with_no_path_arg_default_workspace(self, tmp_path: Path):
        # execute_shell esta em DEFAULT_TO_WORKSPACE — sem cwd, recebe workspace
        assert "execute_shell" in DEFAULT_TO_WORKSPACE
        ok, new_args, err = validate_args(str(tmp_path), "execute_shell", {"command": "ls"})
        assert ok is True
        assert new_args["cwd"] == str(tmp_path.resolve())

    def test_known_tool_no_default_to_workspace_keeps_empty(self, tmp_path: Path):
        # read_file nao esta em DEFAULT_TO_WORKSPACE — sem path, args ficam vazios
        assert "read_file" not in DEFAULT_TO_WORKSPACE
        ok, new_args, err = validate_args(str(tmp_path), "read_file", {})
        assert ok is True
        assert "path" not in new_args


# ─── Path absoluto dentro/fora do workspace ─────────────────────────


class TestAbsolutePathBoundary:
    """Absolute paths são aceitos se dentro do workspace, rejeitados se fora."""

    def test_absolute_path_inside_workspace_accepted(self, tmp_path: Path):
        inside = tmp_path / "inside.txt"
        inside.write_text("x")
        ok, new_args, err = validate_args(
            str(tmp_path), "read_file", {"path": str(inside)}
        )
        assert ok is True
        assert new_args["path"] == str(inside.resolve())
        assert err == ""

    def test_absolute_path_outside_workspace_rejected(self, tmp_path: Path):
        # /etc/passwd existe em todo sistema POSIX; usamos /tmp pra ser cross-OS
        outside = "/tmp"  # garantidamente fora do tmp_path
        ok, _, err = validate_args(str(tmp_path), "read_file", {"path": outside})
        assert ok is False
        assert "outside the agent's workspace" in err
        assert outside in err  # mensagem inclui path original

    def test_absolute_path_sibling_dir_rejected(self, tmp_path: Path):
        # tmp_path.parent contem tmp_path mas paths como tmp_path.parent/other
        # NAO estao em tmp_path
        sibling = tmp_path.parent / "evil"
        ok, _, err = validate_args(str(tmp_path), "read_file", {"path": str(sibling)})
        assert ok is False
        assert "outside" in err.lower()


# ─── Path traversal ─────────────────────────────────────────────────


class TestPathTraversal:
    """Tentativas de escape via `../` devem ser rejeitadas apos resolve()."""

    def test_dot_dot_escape_blocked(self, tmp_path: Path):
        # Relativo "../../etc/passwd" resolvido contra workspace deve sair dele
        ok, _, err = validate_args(
            str(tmp_path), "read_file", {"path": "../../etc/passwd"}
        )
        assert ok is False
        assert "outside" in err.lower()

    def test_dot_dot_loop_back_inside_accepted(self, tmp_path: Path):
        # "subdir/../inside.txt" resolve para workspace/inside.txt — OK
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        ok, new_args, err = validate_args(
            str(tmp_path), "read_file", {"path": "subdir/../inside.txt"}
        )
        assert ok is True
        assert str(tmp_path.resolve() / "inside.txt") == new_args["path"]

    def test_absolute_dot_dot_blocked(self, tmp_path: Path):
        # Path absoluto com traversal: workspace/../escape
        attempt = str(tmp_path) + "/../escape"
        ok, _, err = validate_args(str(tmp_path), "read_file", {"path": attempt})
        assert ok is False
        assert "outside" in err.lower()


# ─── Symlinks ───────────────────────────────────────────────────────


class TestSymlinks:
    """Symlinks são resolvidos via `Path.resolve()`. Se o alvo cai fora
    do workspace, rejeitado."""

    def test_symlink_inside_pointing_outside_rejected(self, tmp_path: Path):
        # Cria symlink dentro do workspace apontando para /tmp (fora)
        outside_target = Path("/tmp")
        link = tmp_path / "evil_link"
        link.symlink_to(outside_target)
        # validate_args resolve o symlink — alvo /tmp esta fora de tmp_path
        ok, _, err = validate_args(str(tmp_path), "read_file", {"path": str(link)})
        assert ok is False
        assert "outside" in err.lower()

    def test_symlink_inside_pointing_inside_accepted(self, tmp_path: Path):
        # Symlink dentro apontando para outro arquivo dentro do workspace
        target = tmp_path / "real.txt"
        target.write_text("x")
        link = tmp_path / "alias"
        link.symlink_to(target)
        ok, new_args, err = validate_args(str(tmp_path), "read_file", {"path": str(link)})
        assert ok is True
        # Resolve retorna o alvo real
        assert new_args["path"] == str(target.resolve())

    def test_symlink_chain_outside_rejected(self, tmp_path: Path):
        # Chain: tmp/A → tmp/B → /tmp/external. Ainda deve resolver e bloquear.
        external = Path("/tmp")
        b = tmp_path / "B"
        b.symlink_to(external)
        a = tmp_path / "A"
        a.symlink_to(b)
        ok, _, err = validate_args(str(tmp_path), "read_file", {"path": str(a)})
        assert ok is False
        assert "outside" in err.lower()


# ─── Relative path resolution ───────────────────────────────────────


class TestRelativePathResolution:
    """Relative paths são resolvidos contra o workspace, NAO contra CWD."""

    def test_relative_path_resolved_against_workspace(self, tmp_path: Path):
        f = tmp_path / "doc.txt"
        f.write_text("x")
        ok, new_args, err = validate_args(
            str(tmp_path), "read_file", {"path": "doc.txt"}
        )
        assert ok is True
        assert new_args["path"] == str(f.resolve())

    def test_tilde_expansion(self, tmp_path: Path):
        # `~` resolve para $HOME via expanduser. Como $HOME esta fora de tmp_path
        # (assumindo HOME != tmp_path), deve ser bloqueado.
        ok, _, err = validate_args(str(tmp_path), "read_file", {"path": "~"})
        assert ok is False
        assert "outside" in err.lower()


# ─── Multiple path params (write_file path + something) ─────────────


class TestMultiplePathParams:
    """Tools como search_files têm um path param; loop processa cada um."""

    def test_search_files_path_validated(self, tmp_path: Path):
        ok, new_args, err = validate_args(
            str(tmp_path), "search_files", {"path": str(tmp_path), "pattern": "foo"}
        )
        assert ok is True
        # path foi reescrito; pattern intocado
        assert new_args["path"] == str(tmp_path.resolve())
        assert new_args["pattern"] == "foo"

    def test_search_files_path_outside_rejected(self, tmp_path: Path):
        ok, _, err = validate_args(
            str(tmp_path), "search_files", {"path": "/tmp", "pattern": "foo"}
        )
        assert ok is False


# ─── Empty / falsy values ───────────────────────────────────────────


class TestEmptyOrFalsyArgs:
    """args[param] = '' ou None: não força workspace (a menos que DEFAULT_TO_WORKSPACE)."""

    def test_empty_path_for_read_file_passes(self, tmp_path: Path):
        # read_file nao esta em DEFAULT_TO_WORKSPACE; path="" deixa intacto
        ok, new_args, err = validate_args(
            str(tmp_path), "read_file", {"path": ""}
        )
        assert ok is True
        assert new_args.get("path", "") == ""

    def test_empty_cwd_for_execute_shell_defaults_to_workspace(self, tmp_path: Path):
        ok, new_args, err = validate_args(
            str(tmp_path), "execute_shell", {"command": "ls"}
        )
        assert ok is True
        assert new_args["cwd"] == str(tmp_path.resolve())

    def test_none_path_default_to_workspace(self, tmp_path: Path):
        ok, new_args, err = validate_args(
            str(tmp_path), "git_operation", {"action": "status", "path": None}
        )
        assert ok is True
        assert new_args["path"] == str(tmp_path.resolve())


# ─── Workspace path expansion ───────────────────────────────────────


class TestWorkspaceExpansion:
    """O próprio `workspace` arg pode ter `~` e ser resolvido."""

    def test_workspace_tilde_expanded(self, tmp_path: Path, monkeypatch):
        # Set HOME para tmp_path para que `~/sub` resolva sob tmp_path
        monkeypatch.setenv("HOME", str(tmp_path))
        subdir = tmp_path / "ws"
        subdir.mkdir()
        ok, new_args, err = validate_args(
            "~/ws", "read_file", {"path": "doc.txt"}
        )
        assert ok is True
        # path resolvido para tmp_path/ws/doc.txt
        assert str(subdir.resolve() / "doc.txt") == new_args["path"]


# ─── Path arg overrides for non-FS tools (sanity) ───────────────────


class TestPathParamsRegistry:
    """Sanity: tools criticos estao registrados em PATH_PARAMS_BY_TOOL."""

    @pytest.mark.parametrize("tool", [
        "read_file", "write_file", "edit_file", "list_directory",
        "search_files", "glob_files", "search_and_replace",
        "execute_shell", "execute_pipeline", "git_operation",
        "run_tests", "project_overview",
    ])
    def test_critical_tool_registered(self, tool: str):
        assert tool in PATH_PARAMS_BY_TOOL, f"{tool} missing from PATH_PARAMS_BY_TOOL"
