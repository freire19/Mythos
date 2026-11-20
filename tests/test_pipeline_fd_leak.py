"""Coverage para FD leak em `_open_redirect_files` (#D004/#008 RES V1.1).

Bug original: se a validacao de workspace falhava no MEIO do loop de
redirects (e.g. `cmd > ok.txt 2> /etc/passwd`), o handle de `ok.txt` ja
estava aberto mas nunca era fechado — `raise ValueError` deixava o dict
orfa. Em pipelines longos isso esgotava o ulimit do processo.

Cobre:
- Erro na PRIMEIRA validacao: nada para fechar
- Erro na SEGUNDA validacao: handle aberto da primeira deve ser fechado
- Tudo ok: handles abertos retornados normalmente
"""

from __future__ import annotations

import gc
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_workspace_leaks():
    yield
    # Forca cleanup pra detector de FD leaks (warnings em stdlib quando
    # file aberto sem close).
    gc.collect()


def _patch_workspace(monkeypatch, ws: Path):
    from alpha.tools import pipeline_tools, workspace as ws_module

    monkeypatch.setattr(pipeline_tools, "AGENT_WORKSPACE", ws)
    monkeypatch.setattr(ws_module, "AGENT_WORKSPACE", ws)


class TestOpenRedirectFiles:
    def test_all_within_workspace_returns_handles(self, tmp_path, monkeypatch):
        from alpha.tools import pipeline_tools

        _patch_workspace(monkeypatch, tmp_path)

        out_path = tmp_path / "out.txt"
        err_path = tmp_path / "err.txt"

        handles = pipeline_tools._open_redirect_files({
            "stdout": str(out_path),
            "stderr": str(err_path),
        })

        try:
            assert "stdout" in handles
            assert "stderr" in handles
            # Os handles devem estar abertos
            handles["stdout"].write("hi")
            handles["stderr"].write("oops")
        finally:
            for fh in handles.values():
                fh.close()

    def test_first_target_outside_workspace_raises_no_leak(
        self, tmp_path, monkeypatch
    ):
        """Se o PRIMEIRO redirect ja falha, nao ha handle pra vazar — mas
        o codigo nao deve crashar mais que o ValueError esperado."""
        from alpha.tools import pipeline_tools

        _patch_workspace(monkeypatch, tmp_path)

        with pytest.raises(ValueError, match="fora do workspace"):
            pipeline_tools._open_redirect_files({
                "stdout": "/etc/passwd",  # primeiro item, fora do workspace
            })

    def test_second_target_outside_workspace_closes_first(
        self, tmp_path, monkeypatch
    ):
        """O bug real: primeiro redirect abre, segundo falha — o primeiro
        DEVE ter sido fechado antes do raise.

        Apos #D117 (O_NOFOLLOW), `_open_redirect_files` usa `os.open` +
        `os.fdopen` em vez de `builtins.open`. Patchamos `os.fdopen`
        em `pipeline_tools` para capturar handles e verificar close().
        """
        from alpha.tools import pipeline_tools

        _patch_workspace(monkeypatch, tmp_path)

        opened_handles: list = []
        import os as _os
        original_fdopen = _os.fdopen

        def tracking_fdopen(*args, **kwargs):
            fh = original_fdopen(*args, **kwargs)
            opened_handles.append(fh)
            return fh

        # Tanto pipeline_tools.os.fdopen quanto os.fdopen direto devem ser
        # patcheados — o helper `_open_redirect_target` faz `os.fdopen(fd, ...)`.
        monkeypatch.setattr(pipeline_tools.os, "fdopen", tracking_fdopen)

        with pytest.raises(ValueError, match="fora do workspace"):
            pipeline_tools._open_redirect_files({
                "stdout": str(tmp_path / "ok.txt"),  # ok
                "stderr": "/etc/passwd",              # falha
            })

        # O handle de ok.txt foi aberto E fechado.
        assert len(opened_handles) >= 1
        for fh in opened_handles:
            assert fh.closed, f"FD vazado: {fh!r} ainda aberto"

    def test_all_targets_outside_workspace_raises_first(
        self, tmp_path, monkeypatch
    ):
        from alpha.tools import pipeline_tools

        _patch_workspace(monkeypatch, tmp_path)

        with pytest.raises(ValueError, match="fora do workspace"):
            pipeline_tools._open_redirect_files({
                "stdout": "/tmp/x",
                "stderr": "/tmp/y",
            })


class TestRedirectTargetNoFollow:
    """#D117: `_open_redirect_target` usa O_NOFOLLOW para fechar TOCTOU.

    A garantia de cleanup (`try/except` + close) ja e coberta
    comportamentalmente por `test_second_target_outside_workspace_closes_first`,
    entao aqui validamos apenas o efeito observavel do O_NOFOLLOW: abrir um
    target que e symlink falha com OSError(ELOOP) em vez de seguir o link.
    """

    @pytest.mark.skipif(
        not hasattr(__import__("os"), "O_NOFOLLOW"),
        reason="O_NOFOLLOW indisponivel nesta plataforma (Windows)",
    )
    def test_symlink_target_raises_eloop(self, tmp_path):
        import errno

        from alpha.tools import pipeline_tools

        real = tmp_path / "real.txt"
        real.write_text("alvo real")
        link = tmp_path / "link.txt"
        link.symlink_to(real)

        with pytest.raises(OSError) as exc:
            pipeline_tools._open_redirect_target("stdout", link)
        # O_NOFOLLOW -> ELOOP (Linux) quando o path final e symlink.
        assert exc.value.errno == errno.ELOOP

    def test_regular_target_opens(self, tmp_path):
        from alpha.tools import pipeline_tools

        target = tmp_path / "out.txt"
        fh = pipeline_tools._open_redirect_target("stdout", target)
        try:
            fh.write("ok")
        finally:
            fh.close()
        # O write foi para o arquivo real (truncado/criado).
        assert target.read_text() == "ok"
