"""Browser tool registrations (#081).

Extracted from browser_tools.py — all register_tool calls in one place
so browser_tools.py stays focused on implementations.
"""

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from .browser_tools import (
    _browser_back,
    _browser_click,
    _browser_close,
    _browser_close_tab,
    _browser_describe_page,
    _browser_execute_js,
    _browser_fill,
    _browser_forward,
    _browser_get_content,
    _browser_list_tabs,
    _browser_navigate,
    _browser_new_tab,
    _browser_open,
    _browser_press_key,
    _browser_query,
    _browser_reload,
    _browser_screenshot,
    _browser_select_option,
    _browser_status,
    _browser_switch_tab,
    _browser_wait_for,
)

_NO_PARAMS = {"type": "object", "properties": {}}


def _reg(name: str, desc: str, params: dict, executor, safety: ToolSafety):
    register_tool(
        ToolDefinition(
            name=name,
            description=desc,
            parameters=params,
            safety=safety,
            category=ToolCategory.BROWSER,
            executor=executor,
        )
    )


_reg(
    "browser_open",
    "Abrir uma sessão persistente de navegador (Chromium). Reutiliza sessão existente.",
    {
        "type": "object",
        "properties": {
            "headless": {
                "type": "boolean",
                "description": "Executar sem interface gráfica",
                "default": True,
            }
        },
    },
    _browser_open,
    ToolSafety.SAFE,
)

_reg(
    "browser_close",
    "Fechar a sessão de navegador e liberar recursos.",
    _NO_PARAMS,
    _browser_close,
    ToolSafety.SAFE,
)

_reg(
    "browser_status",
    "Retornar estado atual da sessão (URL, título, abas).",
    _NO_PARAMS,
    _browser_status,
    ToolSafety.SAFE,
)

_reg(
    "browser_navigate",
    "Navegar a aba ativa para uma URL. Aguarda carregamento da página (com JS).",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL completa (http/https)"},
            "wait_until": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                "default": "load",
            },
            "timeout": {"type": "integer", "description": "Timeout em segundos", "default": 30},
        },
        "required": ["url"],
    },
    _browser_navigate,
    ToolSafety.SAFE,
)

_reg("browser_back", "Voltar para a página anterior no histórico.", _NO_PARAMS, _browser_back, ToolSafety.SAFE)
_reg("browser_forward", "Avançar para a próxima página no histórico.", _NO_PARAMS, _browser_forward, ToolSafety.SAFE)
_reg("browser_reload", "Recarregar a página atual.", _NO_PARAMS, _browser_reload, ToolSafety.SAFE)

_reg(
    "browser_get_content",
    "Obter conteúdo da página atual (texto renderizado por JS ou HTML completo).",
    {
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "enum": ["text", "html"],
                "default": "text",
                "description": "'text' = texto visível; 'html' = HTML completo",
            }
        },
    },
    _browser_get_content,
    ToolSafety.SAFE,
)

_reg(
    "browser_screenshot",
    "Salvar screenshot PNG da página atual no workspace.",
    {
        "type": "object",
        "properties": {
            "save_to": {
                "type": "string",
                "description": "Caminho do arquivo (relativo ao workspace ou absoluto)",
            },
            "full_page": {
                "type": "boolean",
                "description": "Capturar página inteira (não só viewport)",
                "default": False,
            },
        },
    },
    _browser_screenshot,
    ToolSafety.SAFE,
)

_reg(
    "browser_describe_page",
    "Listar elementos interativos visíveis (links, botões, inputs) com seletores prontos para click/fill.",
    _NO_PARAMS,
    _browser_describe_page,
    ToolSafety.SAFE,
)

_reg(
    "browser_query",
    "Consultar elementos por seletor CSS. Retorna texto, visibilidade e atributo opcional.",
    {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "Seletor CSS"},
            "attribute": {
                "type": "string",
                "description": "Atributo HTML para extrair (href, src, value...)",
            },
        },
        "required": ["selector"],
    },
    _browser_query,
    ToolSafety.SAFE,
)

_reg(
    "browser_wait_for",
    "Esperar até que um seletor CSS apareça na página.",
    {
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "timeout": {"type": "integer", "default": 10, "description": "Timeout em segundos"},
        },
        "required": ["selector"],
    },
    _browser_wait_for,
    ToolSafety.SAFE,
)

_reg("browser_list_tabs", "Listar todas as abas abertas com URL e título.", _NO_PARAMS, _browser_list_tabs, ToolSafety.SAFE)

_reg(
    "browser_new_tab",
    "Abrir uma nova aba (opcionalmente navegando para uma URL).",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL inicial (opcional)"}
        },
    },
    _browser_new_tab,
    ToolSafety.SAFE,
)

_reg(
    "browser_switch_tab",
    "Trocar a aba ativa pelo índice.",
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
    "Fechar uma aba pelo índice (ou a ativa se index omitido).",
    {
        "type": "object",
        "properties": {"index": {"type": "integer"}},
    },
    _browser_close_tab,
    ToolSafety.SAFE,
)

_reg(
    "browser_click",
    "Clicar num elemento (seletor CSS). Requer aprovação.",
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
    "Preencher um input/textarea com um valor. Requer aprovação.",
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
    "Selecionar opção em <select> pelo value. Requer aprovação.",
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
    "Pressionar tecla (Enter, Tab, ArrowDown, etc). Pode focar elemento via selector. Requer aprovação.",
    {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Nome da tecla (Playwright keyboard)"},
            "selector": {"type": "string", "description": "Elemento para focar antes (opcional)"},
        },
        "required": ["key"],
    },
    _browser_press_key,
    ToolSafety.DESTRUCTIVE,
)

_reg(
    "browser_execute_js",
    "Executar código JavaScript arbitrário no contexto da página. SEMPRE requer aprovação — risco alto.",
    {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Código JS. Use sintaxe de função flecha: '() => document.title'",
            }
        },
        "required": ["code"],
    },
    _browser_execute_js,
    ToolSafety.DESTRUCTIVE,
)
