"""Code execution tools for ALPHA agent.

SECURITY: A static import blocklist prevents the most dangerous escape vectors
(os, subprocess, shutil, etc.) and project dependencies that enable network
exfiltration (httpx, aiohttp, requests, duckduckgo_search).
The blocklist is best-effort — NOT a real sandbox. Single-user only.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import sys
import tempfile

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ._subprocess_helpers import (
    STDERR_CAP,
    STDOUT_CAP,
    SubprocessTimeoutError,
    run_subprocess_safe,
)
from ..config import TOOL_TIMEOUTS, TOOL_TIMEOUT_CAPS

logger = logging.getLogger(__name__)

# ─── Security ───

VALID_PACKAGE_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._-]*(\[[\w,]+\])?(([=!<>~]=?)[a-zA-Z0-9.*]+)?$"
)

URL_PREFIXES = ("http://", "https://", "git+", "svn+", "ftp://", "/", "\\")

# AST-based blocklists (#D018-PERF + #027/#075/#084 V1.1 + #009 V1.2):
# AST walk linear substituiu a busca regex MULTILINE (175K tentativas no
# pior caso para 5KB de codigo) e tambem elimina falsos positivos como
# `\bopen\s*\(.*(w|a|x)` que casava em `open("read.txt")`. A regex legacy
# foi removida — qualquer extensao do blocklist deve adicionar entradas
# em _BLOCKED_MODULES / _BLOCKED_CALL_NAMES / _BLOCKED_NAME_TOKENS abaixo.
#
# AUDIT V1.2 #009 (RCE crítica corrigida): a tabela anterior listava `os`
# e `subprocess` mas NÃO os módulos *low-level* que esses dois envelopam
# em CPython — `posix`, `nt`, `_posixsubprocess`, `_winapi`. Como a tool
# está em AUTO_APPROVE_TOOLS, `import posix; posix.system("...")` virava
# RCE sem prompt. Esta tabela agora cobre toda a superfície de OS-level
# access que o CPython expõe pra Python puro.
_BLOCKED_MODULES = frozenset({
    # User-facing OS / process / FS modules.
    "os", "subprocess", "shutil", "sys", "importlib", "ctypes", "_ctypes",
    "signal", "code", "multiprocessing", "webbrowser",
    # Low-level OS interfaces — CPython implementation detail of `os` and
    # `subprocess`. Same capabilities, different name (#009 V1.2).
    "posix", "nt", "_posixsubprocess", "_winapi", "msvcrt",
    # Memory / file-descriptor primitives.
    "mmap", "fcntl", "termios", "tty", "pty", "resource", "select",
    # Threading / scheduling that can spawn workers.
    "_thread", "threading", "concurrent", "sched",
    # asyncio.create_subprocess_shell / create_subprocess_exec spawn
    # arbitrary OS processes, bypassing the shell allowlist. execute_python
    # is auto-approved, so an unblocked asyncio = un-prompted RCE
    # (AUDIT_V2.2 #002).
    "asyncio", "_asyncio",
    # Networking — direct + transitive through deps.
    "socket", "_socket", "ssl", "asyncore", "asynchat",
    "http", "urllib", "ftplib", "smtplib", "poplib", "imaplib",
    "telnetlib", "xmlrpc",
    "httpx", "requests", "aiohttp", "urllib3",
    "duckduckgo_search", "ddgs", "dotenv",
    # Serialization that executes arbitrary code on load.
    "pickle", "_pickle", "shelve", "marshal", "dill", "cloudpickle",
    # File-creating sinks that bypass the open()/FileIO checks: sqlite3.connect
    # and dbm.open write arbitrary DB files; archive/compression modules write
    # arbitrary paths. Plant-anywhere via auto-approved execute_python
    # (AUDIT_V2.2 #003). NB: `logging` is NOT blocked here (benign logging.info
    # is common) — its file sinks are blocked in _BLOCKED_FS_METHODS instead.
    "sqlite3", "dbm", "fileinput", "tarfile", "zipfile", "gzip", "bz2", "lzma",
    # Debuggers / profilers / timers that exec arbitrary code strings:
    # pdb.run(s), cProfile.run(s), timeit.timeit(s) all exec a string,
    # re-enabling the open()/__import__ the validator otherwise blocks
    # (AUDIT_V2.2 #004). `antigravity` opens a browser at import time.
    "pdb", "bdb", "cProfile", "profile", "pstats", "trace", "timeit",
    "antigravity", "doctest",
    # Reflection / dynamic loading.
    "runpy", "inspect", "gc", "platform", "dis", "linecache",
    "site", "sysconfig", "modulefinder",
    # Direct access to the builtins namespace defeats the AST guards.
    "builtins", "__builtin__",
    # Low-level user/group/auth lookups.
    "pwd", "grp", "spwd", "crypt", "syslog", "nis",
})

_BLOCKED_CALL_NAMES = frozenset({
    "__import__", "eval", "exec", "compile", "breakpoint",
    "globals", "locals", "getattr", "setattr", "delattr",
    "vars", "chr", "input",
})

# Attribute / name access tokens. These names are dangerous either as
# dotted-attr (`x.__subclasses__`), bare Name (`__subclasses__`), or as
# Subscript constant (`obj["__subclasses__"]`) — all three shapes are
# caught by the validator below.
_BLOCKED_NAME_TOKENS = frozenset({
    "__loader__", "__builtins__", "__subclasses__",
    # __getattribute__ / __getattr__ / __setattr__ allow indirect attribute
    # access that would otherwise be statically visible to the validator.
    # Block them as defense-in-depth even though `__subclasses__` already
    # closes the most common gadget chain (#012 V1.2).
    "__getattribute__", "__getattr__",
    "__setattr__", "__delattr__",
    # `()` chain to enumerate classes used to need __subclasses__, but a
    # well-known alternative is `__init_subclass__.__self_class__` — block
    # the whole metaclass-traversal cluster.
    "__init_subclass__", "__build_class__",
    # Code-object access — could let a determined caller construct a
    # function with a bytecode payload.
    "__code__", "__closure__", "__globals__",
    # AUDIT V1.2 #012: traversal via `().__class__.__bases__[0]` reach into
    # `object` and from there `__subclasses__()` enumerates loaded classes
    # — `subprocess.Popen`, `os.system` reachable mesmo com modulos
    # bloqueados. Bloquear toda a cluster MRO/bases/dict.
    "__class__", "__bases__", "__base__", "__mro__", "__dict__",
    "__import__",
    # SECURITY_TIER0: frame/traceback introspection alcança f_builtins e
    # f_globals sem tocar em nenhum token acima — cadeia confirmada:
    # `raise` → exc.__traceback__.tb_frame.f_builtins["eval"]. Bloquear o
    # cluster inteiro de acesso a frames (traceback, frame, generator,
    # coroutine, async generator).
    "__traceback__", "tb_frame", "tb_next",
    "f_builtins", "f_globals", "f_locals", "f_back",
    "gi_frame", "cr_frame", "ag_frame",
})

# DEEP_SECURITY V3.3 #D123 / #D124: o AST blocklist anterior cobria `os`,
# `subprocess`, `posix`, e `open(path, "w")` em modo Constant — mas deixava
# passar todo o cluster `pathlib.Path` + `io.FileIO` + `tempfile.*` que
# expoe as mesmas primitivas via attribute access. Verificado em runtime
# (audit DEEP_SECURITY V3.3 §"Verificacao em runtime"): `Path("/etc/passwd")
# .read_text()`, `Path.home().joinpath(".bashrc").write_text(...)`,
# `io.FileIO(p, "wb")` e `tempfile.NamedTemporaryFile(mode="w")` eram
# ALLOWED. Combinado com `execute_python` estar em AUTO_APPROVE_TOOLS,
# reabre o vetor plant+execute que V1.5 #006 fechou para write_file/edit_file.
#
# Bloqueamos por nome do atributo na ast.Call.func — pega tanto a forma
# `Path(x).write_text(y)` (Attribute) quanto `io.FileIO(...)` / `tempfile.
# NamedTemporaryFile(...)` que sao Attribute(module, fn).
_BLOCKED_FS_METHODS = frozenset({
    # pathlib.Path — destrutivo
    "write_text", "write_bytes",
    "unlink", "rename", "replace",
    "symlink_to", "hardlink_to",
    "chmod", "lchmod",
    "rmdir", "mkdir", "touch",
    # pathlib.Path — leitura (#D124: exfil arbitrario de /etc/passwd,
    # ~/.ssh/id_rsa, .env, etc. via print(Path(x).read_text()) → tool_result
    # → provider LLM)
    "read_text", "read_bytes",
    # pathlib.Path — traversal/info disclosure
    "iterdir", "glob", "rglob",
    # io module — file constructors que dribram o check de `open()`
    "FileIO",
    # logging file sinks open an arbitrary path for write/append without
    # going through open()/FileIO (AUDIT_V2.2 #003). Blocked by attr name so
    # benign logging.info()/getLogger() still pass.
    "FileHandler", "RotatingFileHandler", "WatchedFileHandler",
    # tempfile module — escreve fora do workspace, plant+execute
    "NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile",
    "TemporaryDirectory", "mkstemp", "mkdtemp",
})


def _validate_code_safety(code: str) -> str | None:
    """Static analysis via AST. Returns error message or None if safe."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Sintaxe invalida e detectada pelo subprocess do executor;
        # nao e nossa responsabilidade aqui. Permitir.
        return None

    for node in ast.walk(tree):
        # import X / from X import ...
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _BLOCKED_MODULES:
                    return _format_block(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in _BLOCKED_MODULES:
                    return _format_block(f"from {node.module} import ...")
        # eval/exec/compile/getattr/chr/__import__/breakpoint(...)
        elif isinstance(node, ast.Call):
            fn = node.func
            fname = None
            if isinstance(fn, ast.Name):
                fname = fn.id
            elif isinstance(fn, ast.Attribute):
                fname = fn.attr
            if fname in _BLOCKED_CALL_NAMES:
                return _format_block(f"{fname}(...)")
            # DEEP_SECURITY V3.3 #D123 / #D124: bloquear FS-touching methods
            # via attribute access (Path.write_text, io.FileIO, tempfile.
            # NamedTemporaryFile, etc.). Compara so o nome do atributo —
            # pega ambas as formas Call(Attribute(Name("Path"), "write_text"))
            # e Call(Attribute(Name("io"), "FileIO")).
            if fname in _BLOCKED_FS_METHODS:
                return _format_block(f"{fname}(...)")
            # DEEP_SECURITY V3.3 #D124: `open()` em qualquer modo expoe
            # leitura arbitraria de arquivos sensiveis fora do workspace
            # (/etc/passwd, ~/.ssh/id_rsa, .env). O sandbox AST nao tem
            # como saber workspace em static analysis — forcamos uso de
            # read_file/write_file tools que validam workspace boundary.
            if fname == "open":
                return _format_block(
                    "open(...) — use as ferramentas read_file/write_file "
                    "(que validam workspace boundary)"
                )
        # #D310: referência bare (Load) a um builtin bloqueado — aliasing
        # (`e=eval;e(...)`), list-subscript (`[exec][0]`) ou `map(eval,...)`
        # driblavam a checagem do func do Call acima. Flagga qualquer Name em
        # contexto Load cujo id seja um call-name bloqueado (mesmo tratamento
        # já dado a __import__).
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in _BLOCKED_CALL_NAMES
        ):
            return _format_block(node.id)
        # __builtins__ / __loader__ / x.__subclasses__()
        elif isinstance(node, ast.Name) and node.id in _BLOCKED_NAME_TOKENS:
            return _format_block(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in _BLOCKED_NAME_TOKENS:
            return _format_block(f".{node.attr}")
        # AUDIT V1.2 #012: Subscript com Constant value bloqueado —
        # `obj["__subclasses__"]` pegava o mesmo gadget que `obj.__subclasses__`
        # mas o validator antigo so cobria `Attribute`/`Name`.
        elif isinstance(node, ast.Subscript):
            sl = node.slice
            if isinstance(sl, ast.Constant) and sl.value in _BLOCKED_NAME_TOKENS:
                return _format_block(f"[{sl.value!r}]")
    return None


def _format_block(snippet: str) -> str:
    return (
        f"Código bloqueado por segurança: '{snippet[:60]}' não é permitido. "
        f"Acesso a módulos de OS (os, subprocess, posix, _posixsubprocess, "
        f"ctypes, mmap, fcntl, etc.) e a primitivas de reflection "
        f"(__getattribute__, __subclasses__, __code__) é bloqueado. "
        f"Use as ferramentas do agente (execute_shell, write_file) "
        f"para operações de sistema."
    )


# ─── Tools ───


async def _execute_python(code: str, timeout: int | None = None) -> dict:
    """Execute Python code in a subprocess with timeout."""
    if timeout is None:
        timeout = TOOL_TIMEOUTS.get("code", 60)
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        return {"error": f"timeout inválido: {timeout!r} (esperado número inteiro)"}
    timeout = min(timeout, TOOL_TIMEOUT_CAPS.get("code", 60))

    # Local execution with static import blocklist
    safety_error = _validate_code_safety(code)
    if safety_error:
        return {"error": safety_error, "blocked": True}

    # #DM014: pre-init para garantir que `finally: os.unlink(script_path)`
    # nao quebre com NameError caso a criacao do tempfile falhe.
    script_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            f.flush()
            script_path = f.name
        try:
            # sandbox=True respects the user's `.alpha/settings.json` setting:
            # passthrough when disabled, firejail/bwrap wrap when enabled. The
            # static import blocklist above (_validate_code_safety) is regex-
            # based and bypassable via obfuscation (chr(), getattr, etc.) —
            # the sandbox is defense-in-depth at the kernel level (no network,
            # private /tmp) for users who opted in to higher-risk workflows.
            r = await run_subprocess_safe(
                sys.executable, "-u", script_path, timeout=timeout,
                sandbox=True,
            )
        except SubprocessTimeoutError:
            return {
                "error": f"Execucao excedeu o timeout de {timeout}s",
                "timeout": True,
            }

        return {
            "exit_code": r.returncode,
            "stdout": r.stdout.decode(errors="replace")[:STDOUT_CAP],
            "stderr": r.stderr.decode(errors="replace")[:STDERR_CAP],
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        if script_path is not None:
            try:
                os.unlink(script_path)
            except OSError as e:
                # #D047: temp script cleanup failing is non-fatal but a silent
                # `pass` hides a leaking /tmp; log at debug for diagnosis.
                logger.debug("could not unlink temp script %s: %s", script_path, e)


async def _install_package(package: str) -> dict:
    """Install a Python package via pip."""
    package = package.strip()

    # Block URL-based installs
    if any(package.lower().startswith(prefix) for prefix in URL_PREFIXES):
        return {
            "error": "Instalação de URLs/caminhos não é permitida. Use apenas nomes de pacotes PyPI."
        }

    # Strict package name validation
    if not VALID_PACKAGE_RE.match(package):
        return {
            "error": f"Nome de pacote inválido: '{package}'. Use formato: 'nome' ou 'nome==versão'"
        }

    # AUDIT_V1.2 #050: rejeitar pip install fora de virtualenv. Sem este check,
    # `pip install ...` em python global tenta `/usr/lib/python*/site-packages/`
    # (sudo required → falha) OU `~/.local/...` (sucesso silencioso). O segundo
    # caso e perigoso: o pacote vai pra location diferente da que o agent usa,
    # entao o proximo `import` falha mesmo com "install ok". Pior: contamina
    # o user-site global do sistema do usuario com pacotes do agent.
    # Opt-out via ALPHA_ALLOW_GLOBAL_PIP=1 para usuarios que sabem o que fazem.
    if sys.prefix == sys.base_prefix and os.environ.get(
        "ALPHA_ALLOW_GLOBAL_PIP", ""
    ).strip().lower() not in ("1", "true", "yes", "on"):
        return {
            "error": (
                "install_package requer um virtualenv ativo. "
                "Ative `.venv` (source .venv/bin/activate) ou rode com "
                "ALPHA_ALLOW_GLOBAL_PIP=1 se realmente quer instalar global."
            ),
            "blocked": True,
        }

    try:
        # #D323: SEM sandbox=True DE PROPÓSITO. O sandbox roda com deny_network
        # por padrão e pip precisa de rede para alcançar o PyPI — encapsular aqui
        # quebraria a instalação. A build do pacote (setup.py/PEP517) roda,
        # portanto, FORA do sandbox; install_package está em REQUIRE_APPROVAL_TOOLS
        # e na SUBAGENT_DESTRUCTIVE_BLOCKLIST, então SEMPRE exige aprovação humana
        # com o nome do pacote visível. Containment de FS mantendo a rede exigiria
        # um override de deny_network em run_subprocess_safe (não implementado).
        r = await run_subprocess_safe(
            sys.executable, "-m", "pip", "install", package,  # DEEP_PERFORMANCE #034: sem --no-cache-dir
            timeout=120,
        )
    except SubprocessTimeoutError:
        return {"error": "Instalacao excedeu timeout de 120s"}

    return {
        "exit_code": r.returncode,
        # pip: stdout capado conservadoramente no mesmo cap de stderr (era 5000).
        "stdout": r.stdout.decode(errors="replace")[:STDERR_CAP],
        "stderr": r.stderr.decode(errors="replace")[:STDERR_CAP],
    }


# ─── Registration ───


register_tool(
    ToolDefinition(
        name="execute_python",
        description="Execute Python code in a separate process. Returns stdout, stderr and exit code.",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
                "timeout": {
                    "type": "integer",
                    "description": (
                        f"Timeout in seconds (max {TOOL_TIMEOUT_CAPS['code']}). "
                        f"Default: {TOOL_TIMEOUTS['code']}"
                    ),
                    "default": TOOL_TIMEOUTS["code"],
                },
            },
            "required": ["code"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        category=ToolCategory.CODE,
        executor=_execute_python,
    )
)

register_tool(
    ToolDefinition(
        name="install_package",
        description="Install a Python package via pip.",
        parameters={
            "type": "object",
            "properties": {
                "package": {
                    "type": "string",
                    "description": "pip package name (e.g. 'requests', 'numpy==1.24')",
                },
            },
            "required": ["package"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        category=ToolCategory.CODE,
        executor=_install_package,
    )
)
