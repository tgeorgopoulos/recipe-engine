# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build: one onedir bundle with two launchers that share it.

    recipe-engine.exe    console  - the CLI (install, check, recipes, export ...) and `serve --stdio`
    recipe-enginew.exe   windowed - what the HKCU Run key starts: `serve` with the tray icon, no console

    pyinstaller recipe-engine.spec  ->  dist/recipe-engine/
    scripts/package.ps1 zips that folder to dist/recipe-engine-win64.zip + .sha256

Playwright's driver and the uiautomation DLL are collected whole; Chrome itself is
the user's own (channel="chrome"), never bundled.
"""
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

block_cipher = None

datas, binaries, hiddenimports = [], [], []
for pkg in ("playwright", "uiautomation", "pystray", "PIL", "uvicorn", "starlette", "anyio", "pydantic"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
# mcp: everything except mcp.cli (it imports typer, which the engine does not ship)
hiddenimports += collect_submodules("mcp", filter=lambda n: not n.startswith("mcp.cli"))
datas += collect_data_files("mcp")
hiddenimports += collect_submodules("recipe_engine") + ["pyvda", "comtypes", "win32crypt", "win32security", "tkinter"]
datas += [("recipe_engine/static", "recipe_engine/static")]
# The protected SOURCE files ride along: invariants.check() reads them from disk at every start
# (it pins mechanisms by their text), so the frozen host self-tests exactly like the checkout.
datas += [("recipe_engine/*.py", "recipe_engine")]

a = Analysis(
    ["recipe_engine/__main__.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["torch", "onnxruntime", "sounddevice", "faster_whisper", "openwakeword", "claude_agent_sdk", "anthropic", "openai"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe_console = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="recipe-engine",
    console=True,
    icon=None,
)
exe_windowed = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="recipe-enginew",
    console=False,
    icon=None,
)
coll = COLLECT(
    exe_console, exe_windowed,
    a.binaries, a.zipfiles, a.datas,
    strip=False,
    upx=False,
    name="recipe-engine",
)
