"""
Bundled-resource access for Alpha (H3 #13 — PyPI readiness).

The old pattern `Path(__file__).resolve().parent.parent / "prompts" / "x"`
resolved to the repo root, which works for `pip install -e .` but breaks
in site-packages installs (the resolved path doesn't contain the data).

`package_data(rel)` walks `importlib.resources` first (the wheel-friendly
path) and falls back to a filesystem read from the source tree when the
package is installed editable or run from a checkout. Both modes return
the same `Path` so callers don't branch.

Use for files bundled inside the `alpha` package (prompts, future
templates). For project-local files (`.env`, `.alpha/settings.json`)
keep using cwd-based resolution — those should be in the user's
project, not the installed package.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path


def package_data(*parts: str) -> Path:
    """Return a Path to a file packaged with the alpha distribution.

    `parts` are interpreted relative to the `alpha` package root, e.g.
    `package_data("prompts", "system.md")` resolves
    `alpha/prompts/system.md` whether the package is editable-installed,
    wheel-installed, or zipped.

    Raises FileNotFoundError if the resource doesn't exist."""
    if not parts:
        raise ValueError("package_data requires at least one path part")

    pkg = "alpha." + ".".join(parts[:-1]) if len(parts) > 1 else "alpha"
    name = parts[-1]
    try:
        ref = resources.files(pkg) / name
        # `ref` may be a MultiplexedPath/Traversable that doesn't expose
        # `Path` directly. Materialize through `as_file` only when needed
        # — for filesystem-installed packages (the common case)
        # `Path(str(ref))` works and avoids the context-manager dance.
        path = Path(str(ref))
        if path.exists():
            return path
    except (ModuleNotFoundError, FileNotFoundError):
        pass

    # Fallback: source-tree layout (editable installs, dev checkouts).
    here = Path(__file__).resolve().parent
    candidate = here.joinpath(*parts)
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"package resource not found: alpha/{'/'.join(parts)}")
