"""`alpha skills ...` subcommand router (Plano-Upgrade-v3 H3 #16).

Lightweight argparse layer over `alpha.skills.manager`. Kept separate
from `main.py` so the main flat-CLI argument parser stays uncluttered;
`main.py` peeks at `sys.argv[1] == "skills"` and hands off here.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from alpha.display import C, c, print_error
from alpha.skills import manager


def _print_install_result(result: manager.InstallResult) -> int:
    for name in result.installed:
        print(c(C.GREEN, f"✓ installed {name}"))
    for name, reason in result.skipped:
        print(c(C.YELLOW, f"⚠ skipped {name}: {reason}"))
    for err in result.errors:
        print(c(C.RED, f"✗ {err}"))

    if not result.installed and not result.skipped and not result.errors:
        print(c(C.DIM, "nothing to do"))
        return 1
    return 0 if not result.errors else 1


def _cmd_install(args: argparse.Namespace) -> int:
    try:
        result = manager.install(
            args.source, name_override=args.name, force=args.force
        )
    except manager.SkillInstallError as e:
        print_error(str(e))
        return 1
    return _print_install_result(result)


def _cmd_list(_: argparse.Namespace) -> int:
    rows = manager.list_installed()
    if not rows:
        print(c(C.DIM, "no skills installed under ~/.alpha/skills/"))
        return 0

    width = max((len(r["name"]) for r in rows), default=0)
    for r in rows:
        name = r["name"].ljust(width)
        if not r["tracked"]:
            tag = c(C.YELLOW, "local-untracked")
            print(f"  {c(C.BOLD, name)}  {tag}")
            continue
        kind = r["kind"]
        source = r["source"]
        commit = f" @ {r['commit']}" if r.get("commit") else ""
        print(f"  {c(C.BOLD, name)}  {c(C.DIM, kind)}  {source}{commit}")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    if manager.remove(args.name):
        print(c(C.GREEN, f"✓ removed {args.name}"))
        return 0
    print_error(f"skill not found: {args.name}")
    return 1


def _cmd_update(args: argparse.Namespace) -> int:
    try:
        result = manager.update(args.name)
    except manager.SkillInstallError as e:
        print_error(str(e))
        return 1
    return _print_install_result(result)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alpha skills",
        description="Manage Alpha skills installed under ~/.alpha/skills/",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    inst = sub.add_parser(
        "install",
        help="Install a skill from a git URL or local path",
        description=(
            "Sources: github:user/repo  |  https://...git  |  /local/path. "
            "Multi-skill repos (each subdir with SKILL.md) are unpacked into "
            "separate entries; --name is only valid for single-skill sources."
        ),
    )
    inst.add_argument("source", help="git URL, github:user/repo, or local path")
    inst.add_argument("--name", help="override the destination directory name")
    inst.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing skill of the same name",
    )
    inst.set_defaults(func=_cmd_install)

    lst = sub.add_parser("list", help="List installed skills with their sources")
    lst.set_defaults(func=_cmd_list)

    rm = sub.add_parser("remove", help="Delete an installed skill")
    rm.add_argument("name")
    rm.set_defaults(func=_cmd_remove)

    upd = sub.add_parser(
        "update",
        help="Re-install tracked skill(s) from their original source",
    )
    upd.add_argument(
        "name",
        nargs="?",
        help="skill name (omit to update every tracked skill)",
    )
    upd.set_defaults(func=_cmd_update)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
