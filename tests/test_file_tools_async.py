"""Regressao para AUDIT_V1.8 #041 — I/O sincrono em file_tools async.

Garante que `_read_file`, `_write_file` e `_edit_file` movem o I/O bloqueante
para thread pool em vez de pendurar o event loop. Sem isso,
`delegate_parallel` com N edits serializa em vez de paralelizar.
"""

from __future__ import annotations

import asyncio

import pytest

from alpha.tools.file_tools import _edit_file, _read_file, _write_file
from alpha.tools.workspace import AGENT_WORKSPACE


@pytest.fixture
def workspace_file(tmp_path_factory):
    """Cria arquivo em area dentro do AGENT_WORKSPACE."""
    target = AGENT_WORKSPACE / "test_file_tools_async_sandbox"
    target.mkdir(exist_ok=True)
    yield target
    import shutil
    shutil.rmtree(target, ignore_errors=True)


@pytest.mark.asyncio
async def test_read_file_non_blocking(workspace_file):
    """Read concorrente nao deve serializar — paralelismo real via to_thread."""
    # Escreve um arquivo grande (>100KB para forcar path de streaming)
    big_file = workspace_file / "big.txt"
    big_file.write_text("x\n" * 100_000)  # ~200KB

    # 5 reads em paralelo + uma coroutine sentinel medindo "yield"
    sentinel_ticks = 0

    async def sentinel():
        nonlocal sentinel_ticks
        for _ in range(50):
            await asyncio.sleep(0)
            sentinel_ticks += 1

    async def do_read():
        return await _read_file(str(big_file))

    # Roda sentinel concorrente com os reads — se reads bloqueassem event
    # loop, sentinel rodaria muito menos ticks.
    sent_task = asyncio.create_task(sentinel())
    results = await asyncio.gather(*(do_read() for _ in range(5)))
    await sent_task

    # Todos os reads ok
    for r in results:
        assert "error" not in r, r
        assert r["total_lines"] == 100_000

    # Sentinel rodou os 50 ticks — event loop ficou livre
    assert sentinel_ticks == 50


@pytest.mark.asyncio
async def test_write_file_non_blocking(workspace_file):
    sentinel_ticks = 0

    async def sentinel():
        nonlocal sentinel_ticks
        for _ in range(30):
            await asyncio.sleep(0)
            sentinel_ticks += 1

    async def do_write(idx: int):
        return await _write_file(
            str(workspace_file / f"out_{idx}.txt"),
            "lorem ipsum\n" * 10_000,
        )

    sent_task = asyncio.create_task(sentinel())
    results = await asyncio.gather(*(do_write(i) for i in range(5)))
    await sent_task

    for r in results:
        assert "error" not in r, r
        assert r["bytes_written"] > 0
    assert sentinel_ticks == 30


@pytest.mark.asyncio
async def test_edit_file_non_blocking(workspace_file):
    target = workspace_file / "edit.txt"
    target.write_text("hello world\n" * 1_000)

    sentinel_ticks = 0

    async def sentinel():
        nonlocal sentinel_ticks
        for _ in range(20):
            await asyncio.sleep(0)
            sentinel_ticks += 1

    sent_task = asyncio.create_task(sentinel())
    # Edit sequenciais sao OK aqui — o ponto e que o yield acontece
    result = await _edit_file(
        str(target), "hello world", "HELLO WORLD", replace_all=True
    )
    await sent_task

    assert "error" not in result, result
    assert result["replaced"] == 1_000
    assert sentinel_ticks == 20


@pytest.mark.asyncio
async def test_edit_file_ambiguous_old_text_errors(workspace_file):
    """old_text repetido sem replace_all deve falhar sem tocar o arquivo."""
    target = workspace_file / "ambiguous.txt"
    content = "foo\nbar\nfoo\n"
    target.write_text(content)

    result = await _edit_file(str(target), "foo", "baz")

    assert "error" in result
    assert "replace_all" in result["error"]
    assert result["occurrences_found"] == 2
    assert target.read_text() == content  # arquivo intocado


@pytest.mark.asyncio
async def test_edit_file_replace_all(workspace_file):
    target = workspace_file / "all.txt"
    target.write_text("foo\nbar\nfoo\nfoo\n")

    result = await _edit_file(str(target), "foo", "qux", replace_all=True)

    assert "error" not in result, result
    assert result["occurrences_found"] == 3
    assert result["replaced"] == 3
    assert target.read_text() == "qux\nbar\nqux\nqux\n"


@pytest.mark.asyncio
async def test_edit_file_single_occurrence_unchanged(workspace_file):
    """Contrato antigo preservado quando old_text e unico."""
    target = workspace_file / "single.txt"
    target.write_text("alpha\nbeta\n")

    result = await _edit_file(str(target), "beta", "gamma")

    assert "error" not in result, result
    assert result["occurrences_found"] == 1
    assert result["replaced"] == 1
    assert target.read_text() == "alpha\ngamma\n"


@pytest.mark.asyncio
async def test_read_file_returns_correct_lines(workspace_file):
    """Verifica que o refactor nao quebrou o contrato de retorno."""
    target = workspace_file / "small.txt"
    target.write_text("line1\nline2\nline3\n")

    result = await _read_file(str(target))
    assert "error" not in result, result
    assert result["total_lines"] == 3
    assert "line1" in result["content"]
    assert "line2" in result["content"]
    assert "line3" in result["content"]


@pytest.mark.asyncio
async def test_read_file_note_truncated(workspace_file):
    """Leitura parcial sinaliza quantas linhas restam e o proximo offset."""
    target = workspace_file / "trunc.txt"
    target.write_text("".join(f"l{i}\n" for i in range(10)))
    result = await _read_file(str(target), offset=0, limit=4)
    assert "truncado" in result.get("note", "")
    assert "offset=4" in result["note"]


@pytest.mark.asyncio
async def test_read_file_note_offset_beyond_eof(workspace_file):
    """Offset alem do fim avisa em prosa (overshoot)."""
    target = workspace_file / "short.txt"
    target.write_text("a\nb\n")  # 2 linhas
    result = await _read_file(str(target), offset=50)
    assert "alem do fim" in result.get("note", "")


@pytest.mark.asyncio
async def test_read_file_note_empty_file_positive_offset(workspace_file):
    """Code-review finding 5: arquivo VAZIO lido com offset>0 ainda avisa."""
    target = workspace_file / "empty.txt"
    target.write_text("")
    result = await _read_file(str(target), offset=5)
    assert result["total_lines"] == 0
    assert "alem do fim" in result.get("note", ""), result
    # E ler um arquivo vazio do inicio NAO produz ruido de note.
    result0 = await _read_file(str(target), offset=0)
    assert "note" not in result0, result0


@pytest.mark.asyncio
async def test_edit_file_text_not_found(workspace_file):
    target = workspace_file / "edit_not_found.txt"
    target.write_text("hello\n")
    result = await _edit_file(str(target), "missing", "x")
    assert "error" in result
    assert "Texto não encontrado" in result["error"]
