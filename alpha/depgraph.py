"""
Dependency graph for codebases — import parsing, DAG construction,
source→sink tracing across files, and circular dependency detection.

Designed for the NARROW phase: "trace every path from user input to
dangerous sink across the entire codebase."
"""

import ast
import logging
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ─── Python import parser ───


def _parse_python_imports(source: str, file_path: str) -> tuple[set[str], set[str], set[str], list[dict]]:
    """Parse Python imports, exports, and call graph from source.

    Returns (local_imports, all_imports, exports, call_graph).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(), set(), set(), []

    imports: set[str] = set()
    all_imports: set[str] = set()
    exports: set[str] = set()
    call_graph: list[dict] = []

    for node in ast.walk(tree):
        # Import statements
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name.split(".")[0]
                imports.add(name)
                all_imports.add(name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                base = node.module.split(".")[0]
                imports.add(base)
                all_imports.add(base)
            for alias in node.names:
                full = f"{node.module}.{alias.name}" if node.module else alias.name
                if alias.name == "*":
                    imports.add(f"{node.module}.*")
                else:
                    imports.add(full)

        # __all__ exports
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        exports.update(
                            elt.value for elt in node.value.elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        )

        # Function/class definitions as exports
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                exports.add(node.name)
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                exports.add(node.name)

        # Call graph: track who calls what
        if isinstance(node, ast.Call):
            caller = _enclosing_function(tree, node)
            callee = _resolve_callee(node)
            if caller and callee:
                call_graph.append({
                    "caller": caller,
                    "callee": callee,
                    "line": node.lineno,
                })

    local_imports = {i for i in imports if not _is_stdlib_or_third_party(i)}
    return local_imports, all_imports, exports, call_graph


def _is_stdlib_or_third_party(name: str) -> bool:
    """Heuristic: is this a stdlib or third-party module (not local)?"""
    stdlib_top = {
        "os", "sys", "re", "json", "time", "datetime", "collections",
        "typing", "io", "pathlib", "logging", "asyncio", "threading",
        "subprocess", "hashlib", "base64", "struct", "math", "random",
        "string", "itertools", "functools", "operator", "enum", "abc",
        "copy", "pickle", "csv", "xml", "html", "http", "urllib",
        "socket", "ssl", "email", "unittest", "argparse", "configparser",
        "dataclasses", "contextlib", "tempfile", "shutil", "glob",
        "fnmatch", "traceback", "warnings", "importlib", "pkgutil",
    }
    return name in stdlib_top or name.startswith(("_", "test_"))


def _enclosing_function(tree: ast.Module, node: ast.AST) -> str | None:
    """Find the enclosing function/class name for a call node."""
    for parent in ast.walk(tree):
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _contains(parent, node):
                # Check if inside a class
                for cls in ast.walk(tree):
                    if isinstance(cls, ast.ClassDef) and _contains(cls, parent):
                        return f"{cls.name}.{parent.name}"
                return parent.name
        elif isinstance(parent, ast.ClassDef):
            if _contains(parent, node):
                # Direct method call in class body
                return parent.name
    return None


def _contains(parent: ast.AST, child: ast.AST) -> bool:
    """Check if parent AST node contains child (by line number)."""
    if not hasattr(parent, "lineno") or not hasattr(parent, "end_lineno"):
        return False
    if not hasattr(child, "lineno"):
        return False
    return parent.lineno <= child.lineno <= (getattr(parent, "end_lineno", parent.lineno))


def _resolve_callee(node: ast.Call) -> str | None:
    """Resolve the name being called."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name):
            return f"{node.func.value.id}.{node.func.attr}"
        return node.func.attr
    return None


# ─── Dependency graph ───


class DependencyGraph:
    """Directed graph of module dependencies for a codebase.

    Supports:
    - Build from Python source tree
    - source→sink path finding across modules
    - Circular import detection
    - Entry point identification
    - Attack surface ranking (most-connected modules)

    Usage:
        dg = DependencyGraph()
        dg.build("/path/to/codebase", glob="**/*.py")
        paths = dg.find_paths("web_request.py", "subprocess.Popen")
        entries = dg.find_entry_points()
    """

    def __init__(self):
        self.graph: dict[str, dict] = {}   # file → {imports, exports, callers, callees}
        self.modules: dict[str, dict] = {} # module_name → {files, exports}
        self.call_graph: list[dict] = []   # all caller→callee edges
        self._source_cache: dict[str, str] = {}  # file → source

    def build(self, root: str, glob_pattern: str = "**/*.py") -> dict:
        """Build dependency graph by parsing all Python files in the codebase.

        Returns stats dict with module count, edge count, and warnings.
        """
        self.graph.clear()
        self.modules.clear()
        self.call_graph.clear()
        self._source_cache.clear()

        base = Path(root)
        files_parsed = 0
        edges = 0
        warnings: list[str] = []
        local_names: set[str] = set()

        # First pass: collect all local module names
        local_names = {
            p.stem for p in base.glob(glob_pattern)
            if not any(x.startswith(".") for x in p.parts)
            and "test" not in p.parts
        }

        # Second pass: parse imports
        for file_path in sorted(base.glob(glob_pattern)):
            if any(p.startswith(".") for p in file_path.parts):
                continue
            if "test" in file_path.parts or file_path.name.startswith("test_"):
                continue

            try:
                source = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            self._source_cache[str(file_path)] = source
            local_imports, all_imports, exports, calls = _parse_python_imports(source, str(file_path))

            rel_path = str(file_path.relative_to(base))
            self.graph[rel_path] = {
                "imports": local_imports & local_names,  # Only local project imports (for graph edges)
                "all_imports": all_imports,              # Full imports (for entry point detection)
                "exports": exports,
                "calls": calls,
            }

            # Register module-level exports
            stem = file_path.stem
            if stem not in self.modules:
                self.modules[stem] = {"files": [], "exports": set()}
            self.modules[stem]["files"].append(rel_path)
            self.modules[stem]["exports"].update(exports)

            self.call_graph.extend(
                {**c, "file": rel_path} for c in calls
            )
            files_parsed += 1
            edges += len(local_imports & local_names)

        # Detect circular imports
        cycles = self._detect_circular_imports(local_names)
        if cycles:
            warnings.append(f"Circular imports detected: {len(cycles)} cycle(s)")

        return {
            "ok": True,
            "files_parsed": files_parsed,
            "modules": len(self.modules),
            "edges": edges,
            "call_edges": len(self.call_graph),
            "circular_imports": cycles,
            "entry_points": self.find_entry_points(),
            "warnings": warnings,
        }

    def _detect_circular_imports(self, local_names: set[str]) -> list[list[str]]:
        """Find circular import cycles using DFS."""
        adj: dict[str, set[str]] = {n: set() for n in local_names}
        for file_path, info in self.graph.items():
            stem = Path(file_path).stem
            for imp in info["imports"]:
                imp_stem = imp.split(".")[0]
                if imp_stem in adj:
                    adj[stem].add(imp_stem)

        cycles = []
        visited: set[str] = set()
        stack: list[str] = []

        def dfs(node: str):
            if node in stack:
                cycle_start = stack.index(node)
                cycles.append(stack[cycle_start:] + [node])
                return
            if node in visited:
                return
            visited.add(node)
            stack.append(node)
            for neighbor in adj.get(node, set()):
                dfs(neighbor)
            stack.pop()

        for n in local_names:
            if n not in visited:
                dfs(n)

        return [c for c in cycles if len(c) > 2]  # Filter trivial self-loops

    # ─── Path finding ───

    def find_paths(
        self,
        source_module: str,
        sink_name: str,
        max_depth: int = 5,
    ) -> list[dict]:
        """Find all call paths from a source module to a sink function.

        Args:
            source_module: Module/file name where data originates (e.g., 'web_request').
            sink_name: Sink function name (e.g., 'subprocess.Popen', 'os.system').
            max_depth: Maximum call depth to search.

        Returns list of paths, each with ['nodes', 'edges', 'depth'].
        """
        # Build adjacency from call graph
        adj: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
        for cg in self.call_graph:
            caller = f"{cg['file']}:{cg['caller']}"
            callee = cg["callee"]
            adj[caller].append((callee, cg["file"], cg["line"]))

        # Find entry points in the source module
        starts = [
            f"{f}:{e}" for f, info in self.graph.items()
            if source_module in f
            for e in info.get("exports", set())
        ]
        if not starts:
            # Fallback: use any function in the module
            starts = [
                f"{cg['file']}:{cg['caller']}"
                for cg in self.call_graph
                if source_module in cg['file']
            ]

        paths: list[dict] = []
        visited_global: set[str] = set()

        for start in starts[:5]:  # Limit starting points
            queue = deque([(start, [start], [])])
            visited: set[str] = set()

            while queue:
                node, node_path, edge_path = queue.popleft()
                if len(node_path) > max_depth:
                    continue
                if node in visited:
                    continue
                visited.add(node)

                # Check if this node matches the sink
                node_name = node.split(":")[-1] if ":" in node else node
                if sink_name.lower() in node_name.lower():
                    paths.append({
                        "source": source_module,
                        "sink": sink_name,
                        "nodes": node_path,
                        "edges": edge_path,
                        "depth": len(node_path),
                    })
                    continue

                for neighbor, file, line in adj.get(node, []):
                    if neighbor not in visited:
                        queue.append((
                            neighbor,
                            node_path + [neighbor],
                            edge_path + [{"file": file, "line": line, "from": node, "to": neighbor}],
                        ))

            visited_global.update(visited)

        # Sort by depth (shorter = more direct path)
        paths.sort(key=lambda p: p["depth"])
        return paths[:20]

    # ─── Entry points ───

    def find_entry_points(self) -> list[dict]:
        """Identify likely attack surface entry points.

        Entry points are modules that:
        1. Import networking/HTTP libraries
        2. Define route handlers or request handlers
        3. Parse user input (argv, env, stdin, sockets)
        4. Are top-level __main__ or CLI entry points
        """
        network_imports = {"socket", "http", "flask", "django", "aiohttp", "fastapi",
                           "tornado", "requests", "httpx", "urllib", "asyncio"}
        input_keywords = {"request", "argv", "environ", "stdin", "input(", "recv",
                          "read(", "query", "param", "body", "header", "cookie", "upload"}

        entries = []
        for file_path, info in self.graph.items():
            score = 0
            reasons: list[str] = []

            # Network imports (use full imports, not just local)
            all_imports_set = info.get("all_imports", info.get("imports", set()))
            if all_imports_set & network_imports:
                score += 3
                reasons.append("network_imports")

            # Input handling
            source = self._source_cache.get(str(Path(file_path)), "")
            source_lower = source.lower()
            for kw in input_keywords:
                if kw in source_lower:
                    score += 1
                    reasons.append(f"input_keyword:{kw}")
                    break

            # Route handlers (decorators like @app.route, @router.get)
            if "@app.route" in source or "@router." in source or "@blueprint" in source:
                score += 5
                reasons.append("route_handler")

            # CLI entry points
            if file_path.endswith("__main__.py") or "def main(" in source or "if __name__" in source:
                score += 2
                reasons.append("cli_entry")

            if score > 0:
                entries.append({
                    "file": file_path,
                    "score": score,
                    "reasons": reasons,
                    "exports": list(info.get("exports", set()))[:20],
                })

        entries.sort(key=lambda e: e["score"], reverse=True)
        return entries[:30]

    # ─── Dangerous sink detection ───

    DANGEROUS_SINKS = {
        # Command execution
        "os.system": "command_injection",
        "os.popen": "command_injection",
        "subprocess.Popen": "command_injection",
        "subprocess.call": "command_injection",
        "subprocess.run": "command_injection",
        "subprocess.check_output": "command_injection",
        "eval": "code_injection",
        "exec": "code_injection",
        "compile": "code_injection",
        # File operations
        "open": "file_operation",
        "shutil.copy": "file_operation",
        "shutil.move": "file_operation",
        "os.remove": "file_operation",
        "os.rename": "file_operation",
        # Deserialization
        "pickle.load": "deserialization",
        "pickle.loads": "deserialization",
        "yaml.load": "deserialization",
        "json.loads": "deserialization",
        "marshal.loads": "deserialization",
        # SQL
        "execute": "sql_injection",
        "executemany": "sql_injection",
        "raw": "sql_injection",
        # Network
        "socket.connect": "ssrf",
        "requests.get": "ssrf",
        "requests.post": "ssrf",
        "httpx.get": "ssrf",
        "httpx.post": "ssrf",
        "urllib.request.urlopen": "ssrf",
    }

    def find_dangerous_sinks(self) -> list[dict]:
        """Find all calls to dangerous sinks in the codebase.

        Returns list of {file, line, caller, callee, sink_type}.
        """
        sinks = []
        for cg in self.call_graph:
            callee = cg["callee"]
            for sink_pattern, sink_type in self.DANGEROUS_SINKS.items():
                if callee == sink_pattern or callee.endswith("." + sink_pattern):
                    sinks.append({
                        "file": cg["file"],
                        "line": cg["line"],
                        "caller": cg["caller"],
                        "callee": callee,
                        "sink_type": sink_type,
                    })
                    break
        return sinks

    # ─── Source→Sink tracing ───

    def trace_all_paths(self) -> list[dict]:
        """Find all paths from entry points to dangerous sinks.

        This is the core NARROW phase operation: identify every
        potential attack path in the codebase.
        """
        entries = self.find_entry_points()
        sinks = self.find_dangerous_sinks()
        all_paths: list[dict] = []

        for entry in entries[:10]:
            entry_module = Path(entry["file"]).stem
            for sink in sinks[:50]:
                sink_name = sink["callee"]
                paths = self.find_paths(entry_module, sink_name, max_depth=6)
                for p in paths:
                    all_paths.append({
                        "entry": entry["file"],
                        "sink_file": sink["file"],
                        "sink_line": sink["line"],
                        "sink": sink_name,
                        "sink_type": sink["sink_type"],
                        "depth": p["depth"],
                        "path": [n.split(":")[-1] for n in p["nodes"]],
                    })

        all_paths.sort(key=lambda p: p["depth"])
        return all_paths[:50]
