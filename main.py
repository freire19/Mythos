#!/usr/bin/env python3
"""
Mythos — Autonomous vulnerability hunter. Red-team cybersecurity agent.

Shim — o CLI real vive em `alpha/cli/entry.py`.

O entry point instalado e `mythos = main:main`; `python main.py ...` e
`python -m alpha` tambem funcionam.
"""

from alpha.cli.entry import main

if __name__ == "__main__":
    main()
