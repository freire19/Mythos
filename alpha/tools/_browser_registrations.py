"""Browser tool registrations.

Auto-generated from browser_tools.py split (#081).
"""

from __future__ import annotations

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from .browser_session import run_on_browser_loop
from .browser_tools import (
    _browser_open, _browser_close, _browser_status,
    _browser_navigate, _browser_back, _browser_forward, _browser_reload,
    _browser_get_content, _browser_screenshot, _browser_describe_page,
    _browser_query, _browser_wait_for,
    _browser_list_tabs, _browser_new_tab, _browser_switch_tab, _browser_close_tab,
    _browser_click, _browser_fill, _browser_select_option,
    _browser_press_key, _browser_execute_js,
    _browser_click_at, _browser_scroll, _browser_type,
    _browser_hover, _browser_drag,
    _browser_read_console, _browser_read_network, _browser_aria_snapshot,
    _browser_upload, _browser_wait_for_navigation,
    _NO_PARAMS,
)

def _reg(name: str, desc: str, params: dict, executor, safety: ToolSafety):
    register_tool(
        ToolDefinition(
            name=name,
            description=desc,
            parameters=params,
            safety=safety,
            category=ToolCategory.BROWSER,
            # Etapa 2: todo executor de browser roda no event loop dedicado
            # (_BrowserLoop) — a sessão sobrevive entre turnos do CLI porque
            # os objetos Playwright nunca trocam de loop.
            executor=run_on_browser_loop(executor),
        )
    )


_reg(
    "browser_open",
    "Open a persistent browser session (Chromium) that survives across turns. "
    "Reuses an existing session. Set profile=true to use the persistent user "
    "profile (~/.alpha/browser-profile): cookies and logins are saved across "
    "Alpha sessions — combine with headless=false so the user can log in once. "
    "When the user set ALPHA_BROWSER_CDP the session ATTACHES to their own running "
    "Chrome: you get a new tab of yours with their cookies/logins; their existing "
    "tabs stay invisible to you; headless/profile are ignored then.",
    {
        "type": "object",
        "properties": {
            "headless": {
                "type": "boolean",
                "description": "Run without a graphical interface",
                "default": True,
            },
            "profile": {
                "type": "boolean",
                "description": (
                    "Use the persistent user profile (saved cookies/logins). "
                    "Default: false, or ALPHA_BROWSER_PROFILE=1"
                ),
            },
        },
    },
    _browser_open,
    ToolSafety.SAFE,
)

_reg(
    "browser_close",
    "Close the browser session and release resources.",
    _NO_PARAMS,
    _browser_close,
    ToolSafety.SAFE,
)

_reg(
    "browser_status",
    "Return the current session state (URL, title, tabs).",
    _NO_PARAMS,
    _browser_status,
    ToolSafety.SAFE,
)

_reg(
    "browser_navigate",
    "Navigate the active tab to a URL. Waits for the page to load (with JS).",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL (http/https)"},
            "wait_until": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                "default": "load",
            },
            "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 30},
        },
        "required": ["url"],
    },
    _browser_navigate,
    ToolSafety.SAFE,
)

_reg("browser_back", "Go back to the previous page in history.", _NO_PARAMS, _browser_back, ToolSafety.SAFE)
_reg("browser_forward", "Go forward to the next page in history.", _NO_PARAMS, _browser_forward, ToolSafety.SAFE)
_reg("browser_reload", "Reload the current page.", _NO_PARAMS, _browser_reload, ToolSafety.SAFE)

_reg(
    "browser_get_content",
    "Get the current page content (JS-rendered text or full HTML).",
    {
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "enum": ["text", "html"],
                "default": "text",
                "description": "'text' = visible text; 'html' = full HTML",
            }
        },
    },
    _browser_get_content,
    ToolSafety.SAFE,
)

_reg(
    "browser_screenshot",
    "Save a PNG screenshot of the current page to the workspace.",
    {
        "type": "object",
        "properties": {
            "save_to": {
                "type": "string",
                "description": "File path (relative to workspace or absolute)",
            },
            "full_page": {
                "type": "boolean",
                "description": "Capture the full page (not just the viewport)",
                "default": False,
            },
        },
    },
    _browser_screenshot,
    ToolSafety.SAFE,
)

_reg(
    "browser_describe_page",
    "List visible interactive elements (links, buttons, inputs) with selectors ready for click/fill.",
    _NO_PARAMS,
    _browser_describe_page,
    ToolSafety.SAFE,
)

_reg(
    "browser_query",
    "Query elements by CSS selector. Returns text, visibility and an optional attribute.",
    {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector"},
            "attribute": {
                "type": "string",
                "description": "HTML attribute to extract (href, src, value...)",
            },
        },
        "required": ["selector"],
    },
    _browser_query,
    ToolSafety.SAFE,
)

_reg(
    "browser_wait_for",
    "Wait until a CSS selector appears on the page.",
    {
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "timeout": {"type": "integer", "default": 10, "description": "Timeout in seconds"},
        },
        "required": ["selector"],
    },
    _browser_wait_for,
    ToolSafety.SAFE,
)

_reg("browser_list_tabs", "List all open tabs with URL and title.", _NO_PARAMS, _browser_list_tabs, ToolSafety.SAFE)

_reg(
    "browser_new_tab",
    "Open a new tab (optionally navigating to a URL).",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Initial URL (optional)"}
        },
    },
    _browser_new_tab,
    ToolSafety.SAFE,
)

_reg(
    "browser_switch_tab",
    "Switch the active tab by index.",
    {
        "type": "object",
        "properties": {"index": {"type": "integer"}},
        "required": ["index"],
    },
    _browser_switch_tab,
    ToolSafety.SAFE,
)

_reg(
    "browser_close_tab",
    "Close a tab by index (or the active one if index is omitted).",
    {
        "type": "object",
        "properties": {"index": {"type": "integer"}},
    },
    _browser_close_tab,
    ToolSafety.SAFE,
)

_reg(
    "browser_click",
    "Click an element (CSS selector). Requires approval.",
    {
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "timeout": {"type": "integer", "default": 10},
        },
        "required": ["selector"],
    },
    _browser_click,
    ToolSafety.DESTRUCTIVE,
)

_reg(
    "browser_fill",
    "Fill an input/textarea with a value. Requires approval.",
    {
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "value": {"type": "string"},
            "timeout": {"type": "integer", "default": 10},
        },
        "required": ["selector", "value"],
    },
    _browser_fill,
    ToolSafety.DESTRUCTIVE,
)

_reg(
    "browser_select_option",
    "Select an option in a <select> by value. Requires approval.",
    {
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "value": {"type": "string"},
            "timeout": {"type": "integer", "default": 10},
        },
        "required": ["selector", "value"],
    },
    _browser_select_option,
    ToolSafety.DESTRUCTIVE,
)

_reg(
    "browser_press_key",
    "Press a key or combo (Enter, Tab, ArrowDown, Control+A, Control+Shift+R). Can focus an element via selector first. Requires approval.",
    {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Key name (Playwright keyboard)"},
            "selector": {"type": "string", "description": "Element to focus first (optional)"},
        },
        "required": ["key"],
    },
    _browser_press_key,
    ToolSafety.DESTRUCTIVE,
)

_reg(
    "browser_execute_js",
    "Execute arbitrary JavaScript in the page context. ALWAYS requires approval — high risk.",
    {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "JS code. Use arrow-function syntax: '() => document.title'",
            }
        },
        "required": ["code"],
    },
    _browser_execute_js,
    ToolSafety.DESTRUCTIVE,
)


# ─── Computer-use (Etapa 3) ───

_reg(
    "browser_click_at",
    "Click at viewport coordinates (x, y) — use after browser_screenshot, "
    "whose result includes the viewport size for calibration. For elements "
    "with a stable CSS selector prefer browser_click.",
    {
        "type": "object",
        "properties": {
            "x": {"type": "number", "description": "X in viewport pixels"},
            "y": {"type": "number", "description": "Y in viewport pixels"},
            "button": {
                "type": "string", "enum": ["left", "right", "middle"],
                "default": "left",
            },
            "click_count": {
                "type": "integer", "description": "1=click, 2=double-click",
                "default": 1,
            },
        },
        "required": ["x", "y"],
    },
    _browser_click_at,
    ToolSafety.DESTRUCTIVE,
)

_reg(
    "browser_scroll",
    "Scroll the page (up/down/left/right by N pixels) or scroll a specific "
    "element into view (selector).",
    {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string", "enum": ["up", "down", "left", "right"],
                "default": "down",
            },
            "amount_px": {"type": "integer", "default": 600},
            "selector": {
                "type": "string",
                "description": "CSS selector to scroll into view (overrides direction)",
            },
        },
    },
    _browser_scroll,
    ToolSafety.SAFE,
)

_reg(
    "browser_type",
    "Type text character-by-character into the FOCUSED element (click first). "
    "For rich-text editors and inputs that reject browser_fill's direct set.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to type (max 5000 chars)"},
            "delay_ms": {
                "type": "integer", "description": "Delay between keys (0-200ms)",
                "default": 20,
            },
        },
        "required": ["text"],
    },
    _browser_type,
    ToolSafety.DESTRUCTIVE,
)

_reg(
    "browser_hover",
    "Hover an element (CSS selector) or viewport coordinates — opens "
    "hover-menus and tooltips.",
    {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector to hover"},
            "x": {"type": "number", "description": "X in viewport pixels (with y)"},
            "y": {"type": "number", "description": "Y in viewport pixels (with x)"},
        },
    },
    _browser_hover,
    ToolSafety.DESTRUCTIVE,
)

_reg(
    "browser_drag",
    "Drag from one viewport coordinate to another (sliders, drag-and-drop, "
    "canvas drawing).",
    {
        "type": "object",
        "properties": {
            "from_x": {"type": "number"},
            "from_y": {"type": "number"},
            "to_x": {"type": "number"},
            "to_y": {"type": "number"},
            "steps": {
                "type": "integer",
                "description": "Intermediate mouse moves (smoothness). Default 12",
                "default": 12,
            },
        },
        "required": ["from_x", "from_y", "to_x", "to_y"],
    },
    _browser_drag,
    ToolSafety.DESTRUCTIVE,
)


# ─── Observability (Etapa 4) ───

_reg(
    "browser_read_console",
    "Read the page's console output (ring buffer of the last 500 entries "
    "across the session). Filter with a regex pattern and/or level "
    "(log|info|warning|error|debug).",
    {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex to filter message text"},
            "level": {
                "type": "string",
                "enum": ["log", "info", "warning", "error", "debug"],
            },
            "limit": {"type": "integer", "default": 50},
        },
    },
    _browser_read_console,
    ToolSafety.SAFE,
)

_reg(
    "browser_read_network",
    "Read observed network requests (ring buffer of the last 500: method, "
    "URL, status, content-type, failures). Filter by URL regex and/or status.",
    {
        "type": "object",
        "properties": {
            "url_pattern": {"type": "string", "description": "Regex to filter URLs"},
            "status": {"type": "integer", "description": "Exact HTTP status (e.g. 500)"},
            "limit": {"type": "integer", "default": 50},
        },
    },
    _browser_read_network,
    ToolSafety.SAFE,
)

_reg(
    "browser_upload",
    "Attach a file FROM THE WORKSPACE to an <input type=file> element.",
    {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector of the file input"},
            "path": {"type": "string", "description": "File path inside the workspace"},
        },
        "required": ["selector", "path"],
    },
    _browser_upload,
    ToolSafety.DESTRUCTIVE,
)

_reg(
    "browser_wait_for_navigation",
    "Wait for the page to reach a load state and/or a URL matching a regex — "
    "use after clicks that trigger navigation.",
    {
        "type": "object",
        "properties": {
            "url_pattern": {"type": "string", "description": "Regex the URL must match"},
            "state": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle"],
                "default": "load",
            },
            "timeout": {"type": "integer", "description": "Seconds (max 60)", "default": 15},
        },
    },
    _browser_wait_for_navigation,
    ToolSafety.SAFE,
)

_reg(
    "browser_aria_snapshot",
    "Accessibility tree snapshot (YAML) of the page or an element — the "
    "precise structural view; prefer over describe_page for complex pages. "
    "Requires Playwright >= 1.49.",
    {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector to snapshot (default: body)",
                "default": "body",
            },
        },
    },
    _browser_aria_snapshot,
    ToolSafety.SAFE,
)
