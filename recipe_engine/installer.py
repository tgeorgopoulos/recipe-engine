"""`recipe-engine install` / `uninstall`: the per-user pieces that make the host a
fixture of this Windows account - nothing outside the profile, no admin.

  install:   data dir + per-install token (ACL: this user only), HKCU Run key
             "RecipeEngine" -> "<recipe-enginew.exe> serve" so the tray host is up
             after sign-in, and the report of where everything lives.
  uninstall: removes the Run key; keeps recipes/token unless --purge.

The Run key name and hive are overridable (RECIPE_ENGINE_RUN_KEY / _RUN_HIVE) so
the tests write to a scratch key, never the real one.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from recipe_engine import auth, config
from recipe_engine.config import log

RUN_KEY_PATH = os.environ.get("RECIPE_ENGINE_RUN_HIVE") or r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = os.environ.get("RECIPE_ENGINE_RUN_KEY") or "RecipeEngine"


def host_exe() -> str:
    """The windowed launcher next to the running executable (frozen) or a python -m fallback."""
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).resolve().parent
        w = here / "recipe-enginew.exe"
        return str(w if w.exists() else Path(sys.executable).resolve())
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else sys.executable}" -m recipe_engine'


def _run_command(exe: str) -> str:
    exe = exe if exe.startswith('"') else f'"{exe}"'
    return f"{exe} serve"


def set_autostart(command: str) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as k:
        winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, command)


def get_autostart() -> Optional[str]:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as k:
            v, _ = winreg.QueryValueEx(k, RUN_VALUE)
            return str(v)
    except OSError:
        return None


def clear_autostart() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE)
            return True
    except OSError:
        return False


def install(exe: Optional[str] = None, autostart: bool = True) -> dict[str, Any]:
    if sys.platform != "win32":
        return {"ok": False, "reason": "Windows only"}
    for d in (config.DATA_DIR, config.RECIPES_DIR, config.TRANSCRIPTS_DIR, config.BROWSER_DOWNLOAD_DIR):
        Path(d).mkdir(parents=True, exist_ok=True)
    token = auth.ensure_token()
    cmd = _run_command(exe or host_exe())
    if autostart:
        set_autostart(cmd)
    out = {"ok": True, "data_dir": str(config.DATA_DIR), "token_file": str(config.TOKEN_FILE), "token_chars": len(token),
           "autostart": cmd if autostart else "", "url": f"http://{config.BIND_HOST}:{config.PORT}"}
    log("INSTALL", f"data {out['data_dir']}; token {out['token_file']}; autostart {'set' if autostart else 'skipped'}")
    return out


def uninstall(purge: bool = False) -> dict[str, Any]:
    if sys.platform != "win32":
        return {"ok": False, "reason": "Windows only"}
    removed = clear_autostart()
    purged = False
    if purge and Path(config.DATA_DIR).exists():
        shutil.rmtree(config.DATA_DIR, ignore_errors=True)
        purged = True
    log("INSTALL", f"autostart {'removed' if removed else 'was not set'}; data {'purged' if purged else 'kept'}")
    return {"ok": True, "autostart_removed": removed, "data_purged": purged, "data_dir": str(config.DATA_DIR)}
