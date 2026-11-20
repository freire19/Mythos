"""Android device tools — test an app on a real phone (or emulator) over adb.

The family mirrors what a person does with the cable plugged in: see the
screen, read what is on it, tap/type, install a build, read the logs.

- `android_devices`     — connected devices, model, Android version, displays
- `android_screenshot`  — capture the screen; the image reaches the model as
                          a vision attachment, or as a Gemini description
                          when the active provider cannot see images
- `android_ui_dump`     — the on-screen elements with text and tap centers
                          (uiautomator) — exact coordinates, no vision needed
- `android_input`       — tap / long_press / swipe / text / key
- `android_app`         — install an APK, launch or stop a package
- `android_logcat`      — recent log lines, optionally for one package

Safety model: everything that only OBSERVES is SAFE; everything that ACTS
on the phone (`android_input`, `android_app`) is DESTRUCTIVE and prompts.
There is deliberately no generic `adb shell` passthrough and no "clear app
data" / uninstall / settings action here — those go through `execute_shell`
where the user sees the literal command.

All adb calls are argv (no shell on the host). Arguments that travel into
the device shell (`input text`, package names) are validated or quoted.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shlex
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool

logger = logging.getLogger(__name__)

_ADB_TIMEOUT = 30.0
_INSTALL_TIMEOUT = 180.0
_MAX_UI_NODES = 120
_MAX_LOG_LINES = 400
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9._:\-]+$")
_DISPLAY_RE = re.compile(r"^\d+$")
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# Keys the model may press by name. Deliberately excludes POWER, the volume
# keys and anything that changes a device setting.
_KEYS = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "enter": "KEYCODE_ENTER",
    "tab": "KEYCODE_TAB",
    "delete": "KEYCODE_DEL",
    "escape": "KEYCODE_ESCAPE",
    "app_switch": "KEYCODE_APP_SWITCH",
    "up": "KEYCODE_DPAD_UP",
    "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT",
    "search": "KEYCODE_SEARCH",
    "menu": "KEYCODE_MENU",
}

_shot_dir: Path | None = None


def _err(msg: str, category: str = "tool_error") -> dict:
    return {"ok": False, "category": category, "error": msg}


def _adb_base(serial: str | None) -> list[str] | dict:
    """`["adb", "-s", serial]` or an error dict."""
    if not shutil.which("adb"):
        return _err(
            "adb não encontrado. Instale o Android platform-tools "
            "(ex.: sudo apt install adb) e conecte o aparelho por USB."
        )
    base = ["adb"]
    serial = (serial or os.getenv("ANDROID_SERIAL", "")).strip()
    if serial:
        if not _SERIAL_RE.match(serial):
            return _err("serial inválido", "invalid_args")
        base += ["-s", serial]
    return base


async def _run(argv: list[str], timeout: float = _ADB_TIMEOUT) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError):
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        raise
    return proc.returncode or 0, out, err


def _adb_failure(err: bytes, out: bytes = b"") -> dict:
    """Turn adb's stderr into an actionable message."""
    text = (err or out).decode(errors="replace").strip()
    low = text.lower()
    if "no devices" in low or "device not found" in low or "no devices/emulators" in low:
        return _err(
            "Nenhum aparelho conectado. Conecte o cabo USB, ligue a "
            "'Depuração USB' (Opções do desenvolvedor) e aceite o aviso "
            "'Permitir depuração USB?' na tela do celular.",
            "no_device",
        )
    if "unauthorized" in low:
        return _err(
            "Aparelho não autorizado: desbloqueie a tela e aceite o aviso "
            "'Permitir depuração USB?'.",
            "no_device",
        )
    if "more than one device" in low:
        return _err(
            "Mais de um aparelho conectado — passe `serial` (veja android_devices).",
            "invalid_args",
        )
    return _err(text[:500] or "adb falhou")


async def _adb(serial: str | None, *args: str, timeout: float = _ADB_TIMEOUT):
    """Run one adb command. Returns (stdout_bytes, None) or (None, error_dict)."""
    base = _adb_base(serial)
    if isinstance(base, dict):
        return None, base
    try:
        code, out, err = await _run([*base, *args], timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        return None, _err(f"adb expirou após {int(timeout)}s", "timeout")
    if code != 0:
        return None, _adb_failure(err, out)
    return out, None


# ─── Devices ────────────────────────────────────────────────────────


async def _display_ids(serial: str | None) -> list[str]:
    out, err = await _adb(serial, "shell", "dumpsys", "SurfaceFlinger", "--display-id")
    if err or not out:
        return []
    return re.findall(r"Display (\d+)", out.decode(errors="replace"))


async def _displays_on_first(serial: str | None) -> list[str]:
    """Physical display ids, the ones that are ON first.

    On a foldable the unused panel is OFF and screencaps as a black image —
    capturing "the first display" on a folded Galaxy Z Fold returned the
    dark inner screen. `dumpsys display` lists each physical display with
    its `mPhysicalDisplayId` followed by its `mState`."""
    ids = await _display_ids(serial)
    out, err = await _adb(serial, "shell", "dumpsys", "display")
    if err or not out:
        return ids
    states = dict(re.findall(
        r"mPhysicalDisplayId=(\d+)\b[\s\S]{0,3000}?\bmState=(\w+)", out.decode(errors="replace")
    ))
    on = [d for d in ids if states.get(d) == "ON"]
    return on + [d for d in ids if d not in on]


async def _android_devices() -> dict:
    out, err = await _adb(None, "devices", "-l")
    if err:
        return err
    devices = []
    for line in out.decode(errors="replace").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        info = {"serial": parts[0], "state": parts[1]}
        for extra in parts[2:]:
            if ":" in extra:
                k, v = extra.split(":", 1)
                if k in ("model", "device", "product"):
                    info[k] = v
        if info["state"] == "device":
            rel, _ = await _adb(parts[0], "shell", "getprop", "ro.build.version.release")
            if rel:
                info["android"] = rel.decode(errors="replace").strip()
            size, _ = await _adb(parts[0], "shell", "wm", "size")
            if size:
                info["screen"] = " ".join(size.decode(errors="replace").split())
            ids = await _display_ids(parts[0])
            if len(ids) > 1:
                info["displays"] = ids
        devices.append(info)
    if not devices:
        return _adb_failure(b"no devices")
    return {"ok": True, "devices": devices}


# ─── Screenshot ─────────────────────────────────────────────────────


def _screenshot_dir() -> Path:
    global _shot_dir
    if _shot_dir is None or not _shot_dir.exists():
        import atexit

        _shot_dir = Path(tempfile.mkdtemp(prefix=f"alpha_android_{os.getuid()}_"))
        os.chmod(_shot_dir, 0o700)
        _d = str(_shot_dir)
        atexit.register(lambda d=_d: shutil.rmtree(d, ignore_errors=True))
    return _shot_dir


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or not data.startswith(_PNG_MAGIC):
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


async def _capture(serial: str | None, display: str | None) -> tuple[bytes | None, dict | None]:
    args = ["exec-out", "screencap", "-p"]
    if display:
        args += ["-d", display]
    out, err = await _adb(serial, *args)
    if err:
        return None, err
    if _png_size(out or b"") is None:
        return None, None  # not an image (foldables print a warning here)
    return out, None


async def _android_screenshot(
    question: str = "", display: str = "", serial: str = "",
) -> dict:
    """Capture the phone screen as a PNG the model can look at."""
    display = str(display or "").strip()
    if display and not _DISPLAY_RE.match(display):
        return _err("display deve ser o id numérico (veja android_devices)", "invalid_args")

    used = display
    tried: list[str] = []
    data: bytes | None = None
    if display:
        data, err = await _capture(serial, display)
        if err:
            return err
    else:
        # Foldables / multi-display: a plain screencap either prints a text
        # warning or silently captures display 0 — on a folded Galaxy Z Fold
        # that is the dark inner panel. With more than one display, walk the
        # ids with the lit one first and keep the first that answers.
        ids = await _displays_on_first(serial)
        if len(ids) > 1:
            for did in ids:
                tried.append(did)
                data, err = await _capture(serial, did)
                if err:
                    return err
                if data is not None:
                    used = did
                    break
        if data is None:
            data, err = await _capture(serial, None)
            if err:
                return err
    if data is None:
        return _err(
            "screencap não devolveu uma imagem"
            + (f" (displays tentados: {', '.join(tried)})" if tried else "")
            + ". A tela está ligada e desbloqueada?"
        )

    path = _screenshot_dir() / f"android_{int(time.time())}_{secrets.token_hex(3)}.png"
    path.write_bytes(data)
    os.chmod(path, 0o600)
    width, height = _png_size(data) or (0, 0)
    result = {
        "ok": True,
        "path": str(path),
        "width": width,
        "height": height,
        "size_bytes": len(data),
        "note": (
            "Coordinates for android_input are pixels of THIS image. For exact "
            "tap targets prefer android_ui_dump."
        ),
        # Executor convention: `_`-prefixed keys leave the JSON text; the
        # image travels as a vision block, or as a Gemini description when
        # the active provider has no vision.
        "_image_path": str(path),
    }
    if used:
        result["display"] = used
    if used and not display:
        others = [d for d in await _display_ids(serial) if d != used]
        if others:
            result["other_displays"] = others
    q = " ".join(str(question or "").split())
    if q:
        result["_image_question"] = q[:500]
    return result


# ─── UI dump ────────────────────────────────────────────────────────


def _parse_ui(xml_text: str, only_interactive: bool) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    nodes: list[dict] = []
    for el in root.iter("node"):
        a = el.attrib
        text = (a.get("text") or "").strip()
        desc = (a.get("content-desc") or "").strip()
        rid = (a.get("resource-id") or "").split("/")[-1]
        clickable = a.get("clickable") == "true"
        editable = (a.get("class") or "").endswith("EditText")
        if not (text or desc or clickable or editable):
            continue
        if only_interactive and not (clickable or editable):
            continue
        m = _BOUNDS_RE.match(a.get("bounds") or "")
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        node: dict = {"x": (x1 + x2) // 2, "y": (y1 + y2) // 2}
        if text:
            node["text"] = text[:120]
        if desc:
            node["desc"] = desc[:120]
        if rid:
            node["id"] = rid
        node["class"] = (a.get("class") or "").split(".")[-1]
        flags = [
            name for name, on in (
                ("clickable", clickable),
                ("editable", editable),
                ("checked", a.get("checked") == "true"),
                ("focused", a.get("focused") == "true"),
                ("disabled", a.get("enabled") == "false"),
                ("password", a.get("password") == "true"),
            ) if on
        ]
        if flags:
            node["flags"] = flags
        if a.get("password") == "true":
            node.pop("text", None)  # never echo a password field's content
        nodes.append(node)
    return nodes


async def _android_ui_dump(only_interactive: bool = False, serial: str = "") -> dict:
    """List what is on screen with tap coordinates (uiautomator)."""
    out, err = await _adb(serial, "exec-out", "uiautomator", "dump", "/dev/tty")
    if err:
        return err
    text = (out or b"").decode(errors="replace")
    start, end = text.find("<?xml"), text.rfind("</hierarchy>")
    if start == -1 or end == -1:
        return _err(
            "uiautomator não devolveu a hierarquia (tela bloqueada, vídeo/jogo em "
            "tela cheia ou animação em curso). Use android_screenshot."
        )
    xml_text = text[start:end + len("</hierarchy>")]
    nodes = _parse_ui(xml_text, bool(only_interactive))
    pkg = re.search(r'package="([^"]+)"', xml_text)
    result: dict = {
        "ok": True,
        "package": pkg.group(1) if pkg else "",
        "count": len(nodes),
        "nodes": nodes[:_MAX_UI_NODES],
    }
    if len(nodes) > _MAX_UI_NODES:
        result["truncated"] = len(nodes) - _MAX_UI_NODES
        result["hint"] = "Passe only_interactive=true para ver só o que é clicável/editável."
    return result


# ─── Input ──────────────────────────────────────────────────────────


def _coord(value, name: str) -> int | dict:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _err(f"{name} deve ser um inteiro (pixels)", "invalid_args")
    if not 0 <= n <= 20000:
        return _err(f"{name} fora do intervalo", "invalid_args")
    return n


def _device_text(text: str) -> str:
    """Quote `text` for `adb shell input text` (device-side `sh`).

    `input text` takes `%s` as a space; the whole argument is single-quoted
    so no character is interpreted by the device shell."""
    return shlex.quote(text.replace("%", "%%").replace(" ", "%s"))


async def _android_input(
    action: str,
    x=None, y=None, x2=None, y2=None,
    text: str = "", key: str = "", duration_ms: int = 300, serial: str = "",
) -> dict:
    """Act on the phone: tap, long_press, swipe, text, key."""
    action = str(action or "").strip().lower()
    try:
        duration = max(50, min(int(duration_ms), 5000))
    except (TypeError, ValueError):
        duration = 300

    if action in ("tap", "long_press", "swipe"):
        cx, cy = _coord(x, "x"), _coord(y, "y")
        for c_ in (cx, cy):
            if isinstance(c_, dict):
                return c_
        if action == "tap":
            cmd = ["input", "tap", str(cx), str(cy)]
        elif action == "long_press":
            cmd = ["input", "swipe", str(cx), str(cy), str(cx), str(cy), str(max(duration, 600))]
        else:
            ex, ey = _coord(x2, "x2"), _coord(y2, "y2")
            for c_ in (ex, ey):
                if isinstance(c_, dict):
                    return c_
            cmd = ["input", "swipe", str(cx), str(cy), str(ex), str(ey), str(duration)]
    elif action == "text":
        if not isinstance(text, str) or not text:
            return _err("text é obrigatório para action=text", "invalid_args")
        if len(text) > 500:
            return _err("text longo demais (máx. 500 caracteres)", "invalid_args")
        if not text.isascii():
            return _err(
                "`adb shell input text` não digita acentos nem emoji. Use texto "
                "sem acento no teste.",
                "invalid_args",
            )
        if any(ord(ch) < 32 for ch in text):
            return _err("text não pode ter quebras de linha — use action=key, key=enter", "invalid_args")
        cmd = ["input", "text", _device_text(text)]
    elif action == "key":
        code = _KEYS.get(str(key or "").strip().lower())
        if not code:
            return _err(f"key deve ser uma de: {', '.join(sorted(_KEYS))}", "invalid_args")
        cmd = ["input", "keyevent", code]
    else:
        return _err("action deve ser tap, long_press, swipe, text ou key", "invalid_args")

    _, err = await _adb(serial, "shell", *cmd)
    if err:
        return err
    return {"ok": True, "action": action, "note": "Confirme o efeito com android_ui_dump ou android_screenshot."}


# ─── App ────────────────────────────────────────────────────────────


async def _android_app(action: str, package: str = "", apk_path: str = "", serial: str = "") -> dict:
    """install an APK, launch or stop a package."""
    action = str(action or "").strip().lower()
    if action == "install":
        from .workspace import assert_within_workspace

        if not apk_path:
            return _err("apk_path é obrigatório para action=install", "invalid_args")
        apk = Path(apk_path).expanduser().resolve()
        ws_err = assert_within_workspace(apk)
        if ws_err:
            return _err(ws_err, "invalid_args")
        if not apk.is_file() or apk.suffix.lower() != ".apk":
            return _err(f"APK não encontrado: {apk}", "invalid_args")
        # -r keeps the app's data on reinstall; never -d (downgrade wipes).
        out, err = await _adb(serial, "install", "-r", str(apk), timeout=_INSTALL_TIMEOUT)
        if err:
            return err
        text = (out or b"").decode(errors="replace").strip()
        if "Success" not in text:
            return _err(text[-500:] or "install falhou")
        return {"ok": True, "action": "install", "apk": str(apk), "output": text[-200:]}

    if not _PACKAGE_RE.match(str(package or "")):
        return _err("package inválido (ex.: com.exemplo.app)", "invalid_args")
    if action == "launch":
        out, err = await _adb(
            serial, "shell", "monkey", "-p", package,
            "-c", "android.intent.category.LAUNCHER", "1",
        )
        if err:
            return err
        text = (out or b"").decode(errors="replace")
        if "No activities found" in text:
            return _err(f"{package} não está instalado ou não tem tela inicial")
        return {"ok": True, "action": "launch", "package": package}
    if action == "stop":
        _, err = await _adb(serial, "shell", "am", "force-stop", package)
        if err:
            return err
        return {"ok": True, "action": "stop", "package": package}
    return _err("action deve ser install, launch ou stop", "invalid_args")


# ─── Logcat ─────────────────────────────────────────────────────────


async def _android_logcat(
    package: str = "", lines: int = 100, level: str = "W", grep: str = "", serial: str = "",
) -> dict:
    """Recent log lines; filter by package and minimum level."""
    level = str(level or "W").strip().upper()[:1]
    if level not in "VDIWEF":
        return _err("level deve ser V, D, I, W, E ou F", "invalid_args")
    try:
        n = max(1, min(int(lines), _MAX_LOG_LINES))
    except (TypeError, ValueError):
        n = 100
    args = ["logcat", "-d", "-v", "time", f"*:{level}"]
    if package:
        if not _PACKAGE_RE.match(package):
            return _err("package inválido (ex.: com.exemplo.app)", "invalid_args")
        pid_out, err = await _adb(serial, "shell", "pidof", package)
        if err:
            # pidof exits 1 when the app is not running — not a device error.
            if err.get("category") == "no_device":
                return err
            pid_out = b""
        pid = (pid_out or b"").decode(errors="replace").split()
        if not pid:
            return {"ok": True, "package": package, "running": False, "lines": [],
                    "note": "O app não está em execução; sem logs por PID. Rode sem `package` para ver o crash."}
        args += ["--pid", pid[0]]
    out, err = await _adb(serial, *args, timeout=45)
    if err:
        return err
    rows = (out or b"").decode(errors="replace").splitlines()
    if grep:
        needle = str(grep).lower()
        rows = [r for r in rows if needle in r.lower()]
    rows = [r[:400] for r in rows[-n:]]
    return {"ok": True, "count": len(rows), "lines": rows}


# ─── Registration ───────────────────────────────────────────────────

_SERIAL_PARAM = {
    "type": "string",
    "description": "Device serial (from android_devices). Only needed with more than one device.",
}

register_tool(ToolDefinition(
    name="android_devices",
    description=(
        "List Android devices connected over adb (serial, model, Android "
        "version, screen size, display ids on foldables). Call this first "
        "when testing an app on a phone."
    ),
    parameters={"type": "object", "properties": {}},
    safety=ToolSafety.SAFE,
    category=ToolCategory.ANDROID,
    executor=_android_devices,
))

register_tool(ToolDefinition(
    name="android_screenshot",
    description=(
        "Capture the Android phone screen so you can SEE it (layout, images, "
        "visual bugs). The image is attached for you; if your model has no "
        "vision, a Gemini description of the screen is attached instead — "
        "pass `question` to tell it what to look for. Shows whatever is on "
        "screen: only capture while the app under test is in the foreground."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "What to check in the image (e.g. 'is there an error banner? what does it say?').",
            },
            "display": {
                "type": "string",
                "description": "Display id on foldables/multi-display devices. Omit to auto-pick.",
            },
            "serial": _SERIAL_PARAM,
        },
    },
    safety=ToolSafety.SAFE,
    category=ToolCategory.ANDROID,
    executor=_android_screenshot,
))

register_tool(ToolDefinition(
    name="android_ui_dump",
    description=(
        "List the elements currently on the Android screen (text, "
        "content-desc, resource id, flags) with the exact x,y center to tap. "
        "Faster and more precise than a screenshot for finding buttons and "
        "reading text. Fails on games/video/WebView-only screens — use "
        "android_screenshot there."
    ),
    parameters={
        "type": "object",
        "properties": {
            "only_interactive": {
                "type": "boolean",
                "description": "Only clickable/editable elements (default false).",
            },
            "serial": _SERIAL_PARAM,
        },
    },
    safety=ToolSafety.SAFE,
    category=ToolCategory.ANDROID,
    executor=_android_ui_dump,
))

register_tool(ToolDefinition(
    name="android_input",
    description=(
        "Act on the Android phone: tap, long_press, swipe, type text, or "
        "press a key (back, home, enter, tab, delete, app_switch, arrows…). "
        "Coordinates are screen pixels — take them from android_ui_dump. "
        "Text must be ASCII (no accents). Only touch the app the user asked "
        "you to test; never send messages, pay, delete data or change device "
        "settings unless the user explicitly told you to."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["tap", "long_press", "swipe", "text", "key"]},
            "x": {"type": "integer", "description": "X in pixels (tap, long_press, swipe start)."},
            "y": {"type": "integer", "description": "Y in pixels (tap, long_press, swipe start)."},
            "x2": {"type": "integer", "description": "Swipe end X."},
            "y2": {"type": "integer", "description": "Swipe end Y."},
            "text": {"type": "string", "description": "Text to type into the focused field (action=text)."},
            "key": {"type": "string", "enum": sorted(_KEYS), "description": "Key to press (action=key)."},
            "duration_ms": {"type": "integer", "description": "Swipe/long-press duration (default 300)."},
            "serial": _SERIAL_PARAM,
        },
        "required": ["action"],
    },
    safety=ToolSafety.DESTRUCTIVE,
    category=ToolCategory.ANDROID,
    executor=_android_input,
))

register_tool(ToolDefinition(
    name="android_app",
    description=(
        "Install an APK from the workspace on the phone (keeps app data), "
        "launch a package, or force-stop it. Does not uninstall or clear data."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["install", "launch", "stop"]},
            "package": {"type": "string", "description": "Package name, e.g. com.example.app (launch/stop)."},
            "apk_path": {"type": "string", "description": "Path to the .apk inside the workspace (install)."},
            "serial": _SERIAL_PARAM,
        },
        "required": ["action"],
    },
    safety=ToolSafety.DESTRUCTIVE,
    category=ToolCategory.ANDROID,
    executor=_android_app,
))

register_tool(ToolDefinition(
    name="android_logcat",
    description=(
        "Read recent Android log lines (logcat). Filter by `package` (running "
        "app), minimum `level` (V/D/I/W/E/F, default W) and a `grep` substring. "
        "Use after reproducing a crash or error."
    ),
    parameters={
        "type": "object",
        "properties": {
            "package": {"type": "string", "description": "Only this app's process."},
            "lines": {"type": "integer", "description": "How many lines from the end (default 100, max 400)."},
            "level": {"type": "string", "enum": ["V", "D", "I", "W", "E", "F"]},
            "grep": {"type": "string", "description": "Case-insensitive substring filter."},
            "serial": _SERIAL_PARAM,
        },
    },
    safety=ToolSafety.SAFE,
    category=ToolCategory.ANDROID,
    executor=_android_logcat,
))
