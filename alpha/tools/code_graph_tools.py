"""
Semantic code search and dependency graph tools for the MAP→NARROW phases.

Tools:
- index_codebase: Build FAISS index + dependency graph for a codebase.
- search_semantic: Semantic search over indexed codebase.
- trace_dataflow: Find paths from entry points to dangerous sinks.
- find_entry_points: Identify likely attack surface entry points.
"""

import asyncio
import logging
from pathlib import Path

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool

logger = logging.getLogger(__name__)

_SECURITY = ToolCategory.SECURITY


# ─── Tools ───


async def index_codebase(
    path: str = ".",
    glob_pattern: str = "**/*.py",
    force_rebuild: bool = False,
) -> dict:
    """Build semantic index and dependency graph for a codebase.

    Chunks the codebase by function/class, generates embeddings, builds
    a FAISS vector index, and constructs the dependency graph for
    source→sink tracing. Required before search_semantic or trace_dataflow.

    Args:
        path: Root directory of the codebase.
        glob_pattern: File pattern to index.
        force_rebuild: Rebuild even if cached index exists.
    """
    root = Path(path).resolve()
    if not root.is_dir():
        return {"ok": False, "error": f"Not a directory: {path}"}

    from alpha.embeddings import chunk_codebase, CodeIndex
    from alpha.depgraph import DependencyGraph

    # Build embeddings index (cached globally, #088)
    idx = getattr(_index_codebase, '_cached_index', None)
    if idx is None:
        idx = CodeIndex()
        _index_codebase._cached_index = idx
    cache_file = root / ".alpha_code_index.pkl"
    embedding_ok = False
    if cache_file.exists() and not force_rebuild:
        if idx.load(str(cache_file)):
            logger.info("Loaded cached index from %s", cache_file)
            embedding_ok = True
        else:
            force_rebuild = True

    if force_rebuild or not idx.chunks:
        chunks = chunk_codebase(str(root), glob_pattern)
        build_result = idx.build(chunks)
        embedding_ok = build_result.get("ok", False)
        if not embedding_ok:
            logger.warning("Embedding index build failed: %s — graph tools still available",
                          build_result.get("error", "unknown"))
    else:
        embedding_ok = True
        try:
            idx.save(str(cache_file))
        except Exception as e:
            logger.warning("Failed to save index: %s", e)

    # Build dependency graph
    dg = DependencyGraph()
    graph_result = dg.build(str(root), glob_pattern)

    # Top security-relevant chunks
    security_chunks = sorted(idx.chunks, key=lambda c: c.get("security_score", 0), reverse=True)[:15]

    return {
        "ok": True,
        "path": str(root),
        "embedding_ok": embedding_ok,
        "total_files": graph_result.get("files_parsed", 0),
        "total_chunks": len(idx.chunks),
        "total_modules": graph_result.get("modules", 0),
        "total_edges": graph_result.get("edges", 0),
        "entry_points": graph_result.get("entry_points", [])[:10],
        "circular_imports": graph_result.get("circular_imports", []),
        "top_security_chunks": [
            {
                "file": c["file"].replace(str(root) + "/", ""),
                "name": c["name"],
                "type": c["type"],
                "start_line": c["start_line"],
                "security_score": c.get("security_score", 0),
            }
            for c in security_chunks
        ],
    }


async def search_semantic(
    path: str = ".",
    query: str = "",
    k: int = 20,
    min_security_score: float = 0.0,
    file_filter: str = "",
) -> dict:
    """Semantic search over indexed codebase.

    Finds code chunks semantically relevant to the query — not just
    grep keyword matching. "where is user input parsed?" finds parsers
    even if they don't contain the exact string "user input".

    Args:
        path: Root of indexed codebase.
        query: Natural language search query.
        k: Number of results.
        min_security_score: Filter by security relevance (0.0-1.0).
        file_filter: Optional glob to restrict search (e.g., '*.py').
    """
    from alpha.embeddings import get_index

    try:
        idx = get_index(str(Path(path).resolve()))
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if not idx.chunks:
        return {"ok": False, "error": "Index is empty — run index_codebase first"}

    results = idx.search(query, k=k, min_security_score=min_security_score)

    if file_filter:
        import fnmatch
        results = [r for r in results if fnmatch.fnmatch(r["file"], file_filter)]

    return {
        "ok": True,
        "query": query,
        "total_indexed": len(idx.chunks),
        "results_count": len(results),
        "results": [
            {
                "file": r["file"].replace(str(Path(path).resolve()) + "/", ""),
                "name": r["name"],
                "type": r["type"],
                "start_line": r["start_line"],
                "end_line": r.get("end_line", r["start_line"]),
                "score": round(r["score"], 4),
                "security_score": r.get("security_score", 0),
                "code_preview": r.get("code", "")[:200],
            }
            for r in results
        ],
    }


async def trace_dataflow(
    path: str = ".",
    source: str = "",
    sink: str = "",
    max_depth: int = 5,
) -> dict:
    """Trace data flow from source module/function to dangerous sink.

    Finds all call paths from a source (e.g., 'web_request' or
    'parse_input') to a dangerous sink (e.g., 'subprocess.Popen',
    'os.system', 'eval', 'open').

    If no source or sink specified, performs full graph analysis:
    traces ALL paths from entry points to dangerous sinks.

    Args:
        path: Root of indexed codebase.
        source: Source module or function name (empty = auto-detect entry points).
        sink: Sink function name (empty = trace to all dangerous sinks).
        max_depth: Maximum call depth to search.
    """
    from alpha.depgraph import DependencyGraph

    dg = DependencyGraph()
    root = str(Path(path).resolve())
    result = dg.build(root)

    if not result.get("ok"):
        return result

    dangerous = dg.find_dangerous_sinks()

    if source and sink:
        paths = dg.find_paths(source, sink, max_depth=max_depth)
        return {
            "ok": True,
            "source": source,
            "sink": sink,
            "paths_found": len(paths),
            "paths": paths[:20],
            "total_dangerous_sinks": len(dangerous),
        }

    # Full analysis: trace all entry points to all dangerous sinks
    all_paths = dg.trace_all_paths()

    return {
        "ok": True,
        "source": source or "(auto-detected entry points)",
        "sink": sink or "(all dangerous sinks)",
        "total_files": result.get("files_parsed", 0),
        "total_modules": result.get("modules", 0),
        "entry_points": result.get("entry_points", [])[:8],
        "dangerous_sinks": [
            {"file": s["file"], "line": s["line"], "caller": s["caller"],
             "callee": s["callee"], "type": s["sink_type"]}
            for s in dangerous[:30]
        ],
        "attack_paths": all_paths[:30],
        "paths_found": len(all_paths),
    }


async def find_entry_points(path: str = ".") -> dict:
    """Identify likely attack surface entry points in a codebase.

    Finds modules that handle network input, parse user data,
    define route handlers, or serve as CLI entry points.
    Ranked by attack surface relevance score.

    Args:
        path: Root directory of the codebase.
    """
    from alpha.depgraph import DependencyGraph

    dg = DependencyGraph()
    result = dg.build(str(Path(path).resolve()))

    if not result.get("ok"):
        return result

    entries = result.get("entry_points", [])

    return {
        "ok": True,
        "total_modules": result.get("modules", 0),
        "entry_points_found": len(entries),
        "entry_points": entries,
    }


# ─── Register ───

_SAFE = ToolSafety.SAFE
_DESTRUCTIVE = ToolSafety.DESTRUCTIVE

CODEGRAPH_TOOLS = [
    ToolDefinition(
        name="index_codebase",
        description="Build semantic index and dependency graph for a codebase. Required before search_semantic or trace_dataflow.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Codebase root directory."},
                "glob_pattern": {"type": "string", "description": "File pattern. Default: **/*.py"},
                "force_rebuild": {"type": "boolean", "description": "Rebuild even if cached."},
            },
            "required": [],
        },
        safety=_SAFE,
        category=_SECURITY,
        executor=index_codebase,
    ),
    ToolDefinition(
        name="search_semantic",
        description="Semantic search over indexed codebase. Finds code by meaning, not keywords. 'where is auth token validated?'",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Codebase root (must be indexed first)."},
                "query": {"type": "string", "description": "Natural language query."},
                "k": {"type": "integer", "description": "Number of results (default: 20)."},
                "min_security_score": {"type": "number", "description": "Security relevance filter 0.0-1.0."},
                "file_filter": {"type": "string", "description": "Glob to restrict files (e.g. '*.py')."},
            },
            "required": ["query"],
        },
        safety=_SAFE,
        category=_SECURITY,
        executor=search_semantic,
    ),
    ToolDefinition(
        name="trace_dataflow",
        description="Trace data flow from entry points to dangerous sinks across the entire codebase. Finds attack paths.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Codebase root."},
                "source": {"type": "string", "description": "Source module (empty = auto-detect entry points)."},
                "sink": {"type": "string", "description": "Sink function (empty = all dangerous sinks)."},
                "max_depth": {"type": "integer", "description": "Max call depth (default: 5)."},
            },
            "required": [],
        },
        safety=_SAFE,
        category=_SECURITY,
        executor=trace_dataflow,
    ),
    ToolDefinition(
        name="find_entry_points",
        description="Identify attack surface entry points: HTTP handlers, input parsers, CLI entry points. Ranked by relevance.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Codebase root."},
            },
            "required": [],
        },
        safety=_SAFE,
        category=_SECURITY,
        executor=find_entry_points,
    ),
]

for td in CODEGRAPH_TOOLS:
    register_tool(td)

logger.info("Code graph tools registered: %d tools", len(CODEGRAPH_TOOLS))
