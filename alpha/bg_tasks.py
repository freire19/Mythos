"""Background task manager — shells de longa duração com status e notificação.

Gap vs Claude Code (Plano-Browser, "Gaps gerais" #4): `execute_shell` só
tinha o detach de GUI (PID solto, output no void). Agora:

- `execute_shell(background=true)` registra a task aqui: processo em nova
  sessão, stdout+stderr num log em `{workspace}/.alpha/runs/bg/<id>.log`.
- Tools `bg_tasks_list` / `bg_task_output` / `bg_task_kill` dão ao modelo
  visibilidade e controle.
- O loop do agente injeta um [ALPHA SYSTEM NOTE] quando uma task termina
  (via `drain_finished_unnotified()`), imitando a notificação de background
  task do Claude Code.

Registro é in-process (escopo da sessão do CLI). Os processos sobrevivem ao
exit do Alpha (start_new_session) — o log fica no disco para post-mortem.
"""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_TASKS = 50  # limite de tasks registradas por sessão (anti-runaway)
_OUTPUT_TAIL_DEFAULT = 4000
_OUTPUT_TAIL_MIN = 256
_OUTPUT_TAIL_MAX = 64_000

_lock = threading.Lock()
_tasks: dict[int, "BgTask"] = {}
_next_id = 1


@dataclass
class BgTask:
    id: int
    command: str
    cwd: str
    proc: subprocess.Popen
    log_path: Path
    started_wall: float = field(default_factory=time.time)
    started_mono: float = field(default_factory=time.monotonic)
    exit_code: int | None = None
    finished_mono: float | None = None
    notified: bool = False

    def poll(self) -> None:
        if self.exit_code is not None:
            return
        code = self.proc.poll()
        if code is not None:
            self.exit_code = code
            self.finished_mono = time.monotonic()

    @property
    def running(self) -> bool:
        self.poll()
        return self.exit_code is None

    @property
    def runtime_s(self) -> float:
        end = self.finished_mono if self.finished_mono is not None else time.monotonic()
        return end - self.started_mono

    def as_dict(self) -> dict:
        self.poll()
        return {
            "task_id": self.id,
            "command": self.command[:200],
            "pid": self.proc.pid,
            "status": "running" if self.exit_code is None else "finished",
            "exit_code": self.exit_code,
            "runtime_s": round(self.runtime_s, 1),
            "log": str(self.log_path),
        }


def _bg_dir() -> Path:
    from .tools.workspace import AGENT_WORKSPACE

    d = Path(AGENT_WORKSPACE) / ".alpha" / "runs" / "bg"
    d.mkdir(parents=True, exist_ok=True)
    return d


def start(command: str, cwd: str, env: dict | None = None) -> dict:
    """Lança `command` (JÁ validado pela tool) em background e registra.

    O caller (shell_tools) é responsável pela validação de segurança do
    comando e do cwd — aqui só execução e registro.
    """
    global _next_id
    try:
        cmd_parts = shlex.split(command)
    except ValueError as e:
        return {"error": f"Comando malformado: {e}"}
    if not cmd_parts:
        return {"error": "Comando vazio"}
    if "|" in cmd_parts:
        return {"error": "Pipes não são suportados em background — divida o comando"}

    with _lock:
        active = sum(1 for t in _tasks.values() if t.running)
        # AUDIT_V2.3 #307: o cap limita CONCORRÊNCIA, não histórico — a
        # versão antiga contava tasks terminadas (nunca recolhidas) e uma
        # sessão longa passava a recusar TODA nova bg task com zero rodando.
        if active >= _MAX_TASKS:
            return {"error": f"Limite de {_MAX_TASKS} background tasks ativas atingido"}
        # Poda terminadas+já-notificadas antigas para o registry não crescer
        # sem limite em sessões longas (mantém as recentes para bg_task_output).
        if len(_tasks) > _MAX_TASKS * 4:
            stale = [
                t.id for t in _tasks.values() if not t.running and t.notified
            ][:_MAX_TASKS]
            for tid in stale:
                _tasks.pop(tid, None)
        task_id = _next_id
        _next_id += 1

    log_path = _bg_dir() / f"task-{os.getpid()}-{task_id}.log"
    try:
        log_fd = open(log_path, "ab")
        try:
            proc = subprocess.Popen(
                cmd_parts,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
        finally:
            # O filho herdou o fd; o nosso handle pode fechar.
            log_fd.close()
    except OSError as e:
        return {"error": f"Falha ao lançar em background: {e}"}

    task = BgTask(
        id=task_id, command=command, cwd=cwd, proc=proc, log_path=log_path
    )
    with _lock:
        _tasks[task_id] = task
    logger.info("Background task #%d started (pid %d): %s", task_id, proc.pid, command[:120])
    return {
        "task_id": task_id,
        "pid": proc.pid,
        "log": str(log_path),
        "active_tasks": active + 1,
        "message": (
            f"Task #{task_id} rodando em background. Consulte com "
            f"bg_task_output(task_id={task_id}) ou bg_tasks_list()."
        ),
    }


def list_tasks() -> list[dict]:
    with _lock:
        return [t.as_dict() for t in _tasks.values()]


def get(task_id: int) -> BgTask | None:
    with _lock:
        return _tasks.get(task_id)


def output(task_id: int, tail_chars: int = _OUTPUT_TAIL_DEFAULT) -> dict:
    task = get(task_id)
    if task is None:
        return {"error": f"Task #{task_id} não existe nesta sessão"}
    info = task.as_dict()
    try:
        size = task.log_path.stat().st_size if task.log_path.is_file() else 0
        if size == 0:
            info["output"] = ""
            info["output_note"] = "log vazio até agora"
            return info
        tail = max(_OUTPUT_TAIL_MIN, min(tail_chars, _OUTPUT_TAIL_MAX))
        with open(task.log_path, "rb") as f:
            if size > tail:
                f.seek(size - tail)
            data = f.read()
        text = data.decode(errors="replace")
        info["output"] = text
        if size > tail:
            info["output_note"] = f"últimos {tail} de {size} bytes (use tail_chars maior se precisar)"
        return info
    except OSError as e:
        info["error"] = f"Falha ao ler log: {e}"
        return info


def kill(task_id: int) -> dict:
    task = get(task_id)
    if task is None:
        return {"error": f"Task #{task_id} não existe nesta sessão"}
    task.poll()
    if task.exit_code is not None:
        return {"task_id": task_id, "status": "finished",
                "exit_code": task.exit_code, "message": "Task já havia terminado"}
    try:
        # Mata o grupo inteiro (start_new_session=True → pgid próprio).
        os.killpg(os.getpgid(task.proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as e:
        return {"error": f"Falha ao terminar task #{task_id}: {e}"}
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if task.proc.poll() is not None:
            break
        time.sleep(0.05)
    if task.proc.poll() is None:
        try:
            os.killpg(os.getpgid(task.proc.pid), signal.SIGKILL)
        except OSError:
            pass
    task.poll()
    return {"task_id": task_id, "status": "killed", "exit_code": task.exit_code}


def drain_finished_unnotified() -> list[dict]:
    """Tasks que terminaram e ainda não foram anunciadas ao modelo.

    Marca como notificadas — cada término gera exatamente um aviso.
    """
    out: list[dict] = []
    with _lock:
        for t in _tasks.values():
            t.poll()
            if t.exit_code is not None and not t.notified:
                t.notified = True
                out.append(t.as_dict())
    return out


def format_notification(finished: list[dict]) -> str:
    """Monta o [ALPHA SYSTEM NOTE] para tasks terminadas."""
    lines = ["[ALPHA SYSTEM NOTE] Background task(s) finished:"]
    for t in finished:
        status = "ok" if t.get("exit_code") == 0 else f"exit {t.get('exit_code')}"
        lines.append(
            f"- Task #{t['task_id']} ({t['command'][:80]}) — {status}, "
            f"{t['runtime_s']}s. Log: bg_task_output(task_id={t['task_id']})."
        )
    lines.append(
        "This is an automated event, not a user message. React only if it "
        "affects the current work; otherwise mention it briefly when done."
    )
    return "\n".join(lines)


def reset_for_tests() -> None:
    """Limpa o registro (testes)."""
    global _next_id
    with _lock:
        _tasks.clear()
        _next_id = 1
