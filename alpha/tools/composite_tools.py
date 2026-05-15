"""Composite tools (macros) — DEPRECATED (#109).

This module is a backward-compatibility shim. Import directly from:
  .project_overview, .run_tests, .search_and_replace, .deploy_check
"""

import warnings
warnings.warn(
    "composite_tools.py is deprecated — import from sub-modules directly.",
    DeprecationWarning, stacklevel=2,
)

from .deploy_check import _deploy_check
from .project_overview import _project_overview
from .run_tests import _run_tests
from .search_and_replace import _search_and_replace

__all__ = [
    "_project_overview",
    "_run_tests",
    "_search_and_replace",
    "_deploy_check",
]
