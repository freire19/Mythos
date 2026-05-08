"""
Semantic code search via embeddings + FAISS vector index.

Chunks code by function/class/method boundaries, generates embeddings
via sentence-transformers (local, offline) with API fallback, and
builds a FAISS index for sub-100ms semantic queries over 100K+ chunks.

Designed for the MAP phase: "find every place in this codebase that
handles user input" → semantic search, not grep.
"""

import ast
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ─── Chunk types ───

CHUNK_FUNCTION = "function"
CHUNK_CLASS = "class"
CHUNK_METHOD = "method"
CHUNK_MODULE = "module"

# ─── Language-specific chunkers ───

_PY_FUNC_RE = re.compile(r"^\s*(async\s+)?def\s+(\w+)", re.MULTILINE)
_PY_CLASS_RE = re.compile(r"^\s*class\s+(\w+)", re.MULTILINE)

# Dangerous pattern keywords for security-focused chunking
_SECURITY_KEYWORDS = {
    "input", "request", "param", "query", "body", "header", "cookie",
    "upload", "file", "read", "write", "exec", "eval", "system",
    "popen", "subprocess", "os.", "open(", "socket", "connect",
    "url", "http", "https", "parse", "decode", "encode", "serialize",
    "deserialize", "unmarshal", "load", "dump", "import", "__",
    "auth", "token", "password", "secret", "key", "crypt", "hash",
    "buffer", "memcpy", "strcpy", "sprintf", "gets", "scanf",
    "sql", "query", "command", "inject", "redirect", "proxy",
}


def _chunk_python(source: str, file_path: str) -> list[dict]:
    """Chunk Python source by function, class, and method boundaries."""
    chunks: list[dict] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Fallback: regex-based chunking
        return _chunk_python_regex(source, file_path)

    lines = source.split("\n")
    module_doc = ast.get_docstring(tree)

    # Module-level chunk (imports, globals, decorators)
    module_body = _extract_module_level(tree, lines)
    if module_body:
        chunks.append({
            "type": CHUNK_MODULE,
            "name": Path(file_path).stem,
            "file": file_path,
            "start_line": 1,
            "end_line": len(lines),
            "code": module_body[:3000],
            "doc": module_doc or "",
        })

    for node in ast.walk(tree):
        chunk = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chunk = _chunk_from_function(node, lines, file_path)
        elif isinstance(node, ast.ClassDef):
            chunk = _chunk_from_class(node, lines, file_path)

        if chunk:
            chunks.append(chunk)

    return chunks


def _chunk_python_regex(source: str, file_path: str) -> list[dict]:
    """Regex-based fallback chunker for files with syntax errors."""
    chunks: list[dict] = []
    lines = source.split("\n")

    # Find all function/class definitions
    positions = []
    for m in _PY_FUNC_RE.finditer(source):
        positions.append((m.start(), "function", m.group(2)))
    for m in _PY_CLASS_RE.finditer(source):
        positions.append((m.start(), "class", m.group(2)))
    positions.sort()

    for i, (pos, kind, name) in enumerate(positions):
        start_line = source[:pos].count("\n") + 1
        end_pos = positions[i + 1][0] if i + 1 < len(positions) else len(source)
        end_line = source[:end_pos].count("\n") + 1
        code = "\n".join(lines[start_line - 1:end_line])

        chunks.append({
            "type": CHUNK_FUNCTION if kind == "function" else CHUNK_CLASS,
            "name": name,
            "file": file_path,
            "start_line": start_line,
            "end_line": end_line,
            "code": code[:3000],
            "doc": "",
        })

    return chunks


def _chunk_from_function(node: ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str], file_path: str) -> dict:
    name = node.name
    doc = ast.get_docstring(node) or ""
    code = "\n".join(lines[node.lineno - 1:node.end_lineno])
    # Determine if method (inside class)
    chunk_type = CHUNK_METHOD if "." in name or any(
        isinstance(p, ast.ClassDef) for p in ast.walk(ast.Module(body=[node], type_ignores=[]))
    ) else CHUNK_FUNCTION
    return {
        "type": chunk_type,
        "name": name,
        "file": file_path,
        "start_line": node.lineno,
        "end_line": node.end_lineno,
        "code": code[:3000],
        "doc": doc,
        "decorators": [ast.unparse(d) for d in node.decorator_list] if node.decorator_list else [],
        "args": [a.arg for a in node.args.args],
    }


def _chunk_from_class(node: ast.ClassDef, lines: list[str], file_path: str) -> dict:
    doc = ast.get_docstring(node) or ""
    code = "\n".join(lines[node.lineno - 1:node.end_lineno])
    methods = [
        n.name for n in node.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return {
        "type": CHUNK_CLASS,
        "name": node.name,
        "file": file_path,
        "start_line": node.lineno,
        "end_line": node.end_lineno,
        "code": code[:3000],
        "doc": doc,
        "bases": [ast.unparse(b) for b in node.bases],
        "methods": methods,
    }


def _extract_module_level(tree: ast.Module, lines: list[str]) -> str:
    """Extract module-level code (imports, globals) before first function/class."""
    first_def = len(lines)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first_def = min(first_def, node.lineno - 1)
    return "\n".join(lines[:first_def])[:3000]


# ─── Chunk dispatcher ───

_CHUNKERS = {
    ".py": _chunk_python,
    ".pyx": _chunk_python,
    ".pxd": _chunk_python,
}


def chunk_file(file_path: str) -> list[dict]:
    """Chunk a single file by function/class boundaries. Returns list of chunks."""
    path = Path(file_path)
    ext = path.suffix.lower()
    chunker = _CHUNKERS.get(ext)
    if not chunker:
        # Generic: whole file as one chunk
        try:
            code = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []
        return [{
            "type": CHUNK_MODULE,
            "name": path.stem,
            "file": str(path),
            "start_line": 1,
            "end_line": code.count("\n") + 1,
            "code": code[:3000],
            "doc": "",
        }]
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return chunker(source, str(path))


def chunk_codebase(root: str, glob_pattern: str = "**/*.py") -> list[dict]:
    """Recursively chunk all files in a codebase matching the glob pattern.

    Args:
        root: Root directory of the codebase.
        glob_pattern: Glob pattern for files to index.

    Returns flat list of chunks, each with file, line range, code, and metadata.
    """
    chunks: list[dict] = []
    base = Path(root)
    for file_path in sorted(base.glob(glob_pattern)):
        if any(p.startswith(".") for p in file_path.parts):
            continue
        if "test" in file_path.parts or file_path.name.startswith("test_"):
            continue
        if file_path.name.startswith("__") and file_path.name.endswith("__.py"):
            # Include __init__.py, skip __pycache__
            pass
        try:
            file_chunks = chunk_file(str(file_path))
            for c in file_chunks:
                c["id"] = _chunk_id(c)
                c["security_score"] = _security_relevance(c)
            chunks.extend(file_chunks)
        except Exception as e:
            logger.debug("Failed to chunk %s: %s", file_path, e)
    return chunks


def _chunk_id(chunk: dict) -> str:
    """Stable ID for a chunk: hash of file + name + start_line."""
    key = f"{chunk['file']}:{chunk['name']}:{chunk['start_line']}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _security_relevance(chunk: dict) -> float:
    """Score 0.0-1.0 how relevant a chunk is to security analysis."""
    code_lower = chunk.get("code", "").lower()
    name_lower = chunk.get("name", "").lower()
    combined = f"{name_lower} {code_lower}"
    hits = sum(1 for kw in _SECURITY_KEYWORDS if kw in combined)
    return min(hits / 10.0, 1.0)


# ─── Embedding engine ───

_embedding_model: Any = None
_embedding_dim: int = 384  # default for all-MiniLM-L6-v2


def _get_local_model():
    """Lazy-load sentence-transformers model (all-MiniLM-L6-v2, ~80MB)."""
    global _embedding_model, _embedding_dim
    if _embedding_model is not None:
        return _embedding_model
    try:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
        _embedding_dim = _embedding_model.get_sentence_embedding_dimension()
        logger.info("Loaded local embedding model: all-MiniLM-L6-v2 (%d dims)", _embedding_dim)
        return _embedding_model
    except ImportError:
        logger.debug("sentence-transformers not installed")
        return None
    except Exception as e:
        logger.warning("Failed to load local embedding model: %s", e)
        return None


async def _embed_api(texts: list[str], api_key: str | None = None) -> list[list[float]] | None:
    """Use OpenAI-compatible embeddings API as fallback."""
    import os
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": "text-embedding-3-small", "input": texts},
            )
            if resp.status_code == 200:
                data = resp.json()
                return [d["embedding"] for d in data["data"]]
    except Exception as e:
        logger.debug("API embedding failed: %s", e)
    return None


def _chunk_text(chunk: dict) -> str:
    """Convert a chunk to searchable text for embedding."""
    parts = [
        f"[{chunk.get('type', 'code')}] {chunk.get('name', '')}",
    ]
    if chunk.get("doc"):
        parts.append(chunk["doc"])
    if chunk.get("args"):
        parts.append(f"Args: {', '.join(chunk['args'])}")
    if chunk.get("decorators"):
        parts.append(f"Decorators: {', '.join(chunk['decorators'])}")
    parts.append(chunk.get("code", ""))
    return " ".join(parts)


# ─── FAISS index wrapper ───

_faiss_available: bool | None = None


def _has_faiss() -> bool:
    global _faiss_available
    if _faiss_available is None:
        try:
            import faiss  # noqa: F401
            _faiss_available = True
        except ImportError:
            _faiss_available = False
    return _faiss_available


class CodeIndex:
    """FAISS-backed semantic search index for code chunks.

    Usage:
        index = CodeIndex()
        index.build(chunks)           # chunk_codebase() output
        results = index.search("buffer overflow in HTTP parser", k=20)
        for r in results:
            print(r["file"], r["start_line"], r["score"])
    """

    def __init__(self):
        self.chunks: list[dict] = []
        self.index: Any = None  # faiss.IndexFlatIP
        self._dim: int | None = None

    def build(self, chunks: list[dict], use_api: bool = False) -> dict:
        """Build FAISS index from chunks. Returns stats dict."""
        self.chunks = chunks
        if not chunks:
            return {"ok": False, "error": "No chunks to index", "total_chunks": 0}

        # Generate embeddings
        texts = [_chunk_text(c) for c in chunks]
        embeddings = self._embed(texts, use_api=use_api)
        if embeddings is None:
            return {"ok": False, "error": "No embedding model available", "total_chunks": len(chunks)}

        self._dim = len(embeddings[0])

        # Build FAISS index
        if _has_faiss():
            import numpy as np
            import faiss
            arr = np.array(embeddings, dtype=np.float32)
            self.index = faiss.IndexFlatIP(self._dim)  # inner product = cosine for normalized
            self.index.add(arr)
            return {
                "ok": True,
                "total_chunks": len(chunks),
                "dim": self._dim,
                "index_type": "faiss",
                "model": "local" if not use_api else "api",
            }

        # No FAISS fallback: brute-force numpy
        import numpy as np
        self._embeddings = np.array(embeddings, dtype=np.float32)
        self._norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True)
        self._embeddings_norm = self._embeddings / (self._norms + 1e-8)
        return {
            "ok": True,
            "total_chunks": len(chunks),
            "dim": self._dim,
            "index_type": "numpy_bruteforce",
            "model": "local" if not use_api else "api",
        }

    def _embed(self, texts: list[str], use_api: bool = False) -> list[list[float]] | None:
        """Generate embeddings using local model or API."""
        # Try local model first (synchronous, no event loop issues)
        model = _get_local_model()
        if model and not use_api:
            return model.encode(texts, show_progress_bar=False).tolist()

        # Try API (async, needs careful event loop handling)
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            # Already inside an async context — can't use run_until_complete
            if model:
                return model.encode(texts, show_progress_bar=False).tolist()
            return None
        except RuntimeError:
            # No running loop — safe to use run_until_complete
            loop = asyncio.new_event_loop()
            try:
                embeddings = loop.run_until_complete(_embed_api(texts))
                if embeddings:
                    return embeddings
            finally:
                loop.close()

        # Last resort: local model
        if model:
            return model.encode(texts, show_progress_bar=False).tolist()

        return None

    def search(self, query: str, k: int = 20, min_security_score: float = 0.0) -> list[dict]:
        """Semantic search for code chunks matching the query.

        Args:
            query: Natural language query (e.g., "where does user input enter the system?")
            k: Number of results.
            min_security_score: Filter by minimum security relevance (0.0-1.0).

        Returns list of chunks with added 'score' field, sorted by relevance.
        """
        if not self.chunks or self._dim is None:
            return []

        query_vec = self._embed([query])
        if query_vec is None:
            return []
        import numpy as np
        q = np.array(query_vec[0], dtype=np.float32).reshape(1, -1)

        if self.index is not None:
            import faiss
            scores, indices = self.index.search(q, min(k * 2, len(self.chunks)))
        else:
            # Brute-force cosine similarity
            q_norm = q / (np.linalg.norm(q) + 1e-8)
            scores = np.dot(self._embeddings_norm, q_norm.T).flatten()
            indices = np.argsort(scores)[::-1][:k * 2]
            indices = indices.reshape(1, -1)
            scores = scores[indices].reshape(1, -1)

        results = []
        for i, idx in enumerate(indices[0]):
            if idx >= len(self.chunks) or idx < 0:
                continue
            chunk = dict(self.chunks[idx])
            chunk["score"] = float(scores[0][i])
            if chunk.get("security_score", 0) < min_security_score:
                continue
            results.append(chunk)
            if len(results) >= k:
                break

        return results

    def save(self, path: str) -> None:
        """Persist index to disk."""
        import pickle
        data = {
            "chunks": self.chunks,
            "dim": self._dim,
        }
        with open(path, "wb") as f:
            pickle.dump(data, f)
        if self.index is not None and _has_faiss():
            import faiss
            faiss.write_index(self.index, path + ".faiss")

    def load(self, path: str) -> bool:
        """Load index from disk. Returns True if successful."""
        import pickle
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            self.chunks = data["chunks"]
            self._dim = data["dim"]
            if _has_faiss():
                import faiss
                faiss_path = path + ".faiss"
                if Path(faiss_path).exists():
                    self.index = faiss.read_index(faiss_path)
            return True
        except Exception as e:
            logger.warning("Failed to load index from %s: %s", path, e)
            return False


# ─── Module-level helpers ───

_code_index_cache: dict[str, CodeIndex] = {}


def get_index(codebase_path: str, force_rebuild: bool = False) -> CodeIndex:
    """Get or build a CodeIndex for a codebase. Cached by path."""
    key = str(Path(codebase_path).resolve())
    if key not in _code_index_cache or force_rebuild:
        idx = CodeIndex()
        cache_file = Path(codebase_path) / ".alpha_code_index.pkl"
        if cache_file.exists() and not force_rebuild:
            if idx.load(str(cache_file)):
                _code_index_cache[key] = idx
                return idx
        chunks = chunk_codebase(codebase_path)
        idx.build(chunks)
        try:
            idx.save(str(cache_file))
        except Exception:
            pass
        _code_index_cache[key] = idx
    return _code_index_cache[key]
