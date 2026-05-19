"""Bundled data files shipped inside the wheel.

This subpackage exists so `[tool.setuptools.package-data]` has a Python
namespace to attach non-code files (agent YAML profiles today, possibly
curated skill defaults later). Without an `__init__.py` here, setuptools
wouldn't include `alpha/data/**/*` in the wheel.

Empty by design — discovery happens through filesystem walks from
`alpha/agents/registry.py` and friends.
"""
