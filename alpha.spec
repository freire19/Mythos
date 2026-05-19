# PyInstaller spec for the standalone Alpha binary (Plano-v3 §4 Tier 2).
#
# Build:   pyinstaller alpha.spec
# Output:  dist/alpha           (single-file executable, ~75MB on Linux)
#
# The spec lives next to pyproject.toml so the build is reproducible
# without remembering CLI flags. Optional extras (playwright, asyncpg,
# aiohttp, trafilatura, pypdf) are excluded so the default binary stays
# lean — users who need those features should `pipx install alpha-code[...]`
# or edit the excludes list and rebuild.
#
# To produce a binary WITH a specific extra:
#   pip install '.[browser]'   # or [multimodal], etc.
#   Then remove the entry from `excludes` below and rebuild.

# ruff: noqa  (spec files run inside PyInstaller's namespace — Analysis,
# PYZ, EXE are injected globals, not imports)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # alpha/prompts/*.md is the only runtime data the binary needs —
    # system.md and subagent.md ship inside the wheel via package_data
    # (H3 #13). PyInstaller doesn't see [tool.setuptools.package-data]
    # so we have to mirror the entry here.
    datas=[('alpha/prompts', 'alpha/prompts')],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Optional extras from pyproject.toml — keep the default binary
        # lean. PyInstaller drags these in if they're installed in the
        # build venv, even when the agent only lazy-imports them.
        'playwright',
        'aiohttp',
        'asyncpg',
        'trafilatura',
        'pypdf',
        # GUI / desktop frameworks never used by the terminal agent.
        'tkinter',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        # IPython/Jupyter sometimes pulled in transitively by dev deps.
        'IPython', 'jupyter', 'notebook',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='alpha',
    debug=False,
    bootloader_ignore_signals=False,
    # UPX shrinks the binary by ~30% but adds 1-2s to cold start as
    # the executable decompresses itself. Off by default — flip to
    # True after `apt install upx-ucl` if size matters more than
    # startup latency for the distribution channel.
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
