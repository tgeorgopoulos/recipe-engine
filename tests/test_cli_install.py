"""`recipe-engine install` / `uninstall` against a SCRATCH HKCU key and a temp data
dir: the real Run key and the real token are never touched."""
from __future__ import annotations

import sys

import pytest

from recipe_engine import config

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


@pytest.fixture
def scratch(monkeypatch, tmp_path):
    from recipe_engine import installer
    monkeypatch.setattr(installer, "RUN_KEY_PATH", r"Software\RecipeEngineTests\Run")
    monkeypatch.setattr(installer, "RUN_VALUE", "RecipeEngineTest")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "RECIPES_DIR", tmp_path / "data" / "recipes")
    monkeypatch.setattr(config, "TRANSCRIPTS_DIR", tmp_path / "data" / "transcripts")
    monkeypatch.setattr(config, "BROWSER_DOWNLOAD_DIR", tmp_path / "data" / "downloads")
    monkeypatch.setattr(config, "TOKEN_FILE", tmp_path / "data" / "token")
    yield installer
    installer.clear_autostart()
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\RecipeEngineTests\Run")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\RecipeEngineTests")
    except OSError:
        pass


def test_install_creates_dirs_token_and_run_key_then_uninstall_removes_the_key(scratch, tmp_path):
    installer = scratch
    out = installer.install(exe=r"C:\Program Files\X\recipe-enginew.exe")
    assert out["ok"] and (tmp_path / "data" / "recipes").is_dir() and (tmp_path / "data" / "token").exists()
    assert out["token_chars"] >= 32
    assert installer.get_autostart() == r'"C:\Program Files\X\recipe-enginew.exe" serve'
    assert installer.install(exe=r"C:\Program Files\X\recipe-enginew.exe")["ok"], "install is idempotent"
    token_before = (tmp_path / "data" / "token").read_text()
    installer.install(exe=r"C:\Program Files\X\recipe-enginew.exe")
    assert (tmp_path / "data" / "token").read_text() == token_before, "a re-install must not rotate the token"
    un = installer.uninstall()
    assert un["ok"] and un["autostart_removed"] is True and installer.get_autostart() is None
    assert (tmp_path / "data" / "token").exists(), "uninstall keeps the data unless --purge"
    assert installer.uninstall(purge=True)["data_purged"] is True and not (tmp_path / "data").exists()


def test_no_autostart_flag_skips_the_run_key(scratch):
    out = scratch.install(exe=r"C:\X\recipe-enginew.exe", autostart=False)
    assert out["ok"] and out["autostart"] == "" and scratch.get_autostart() is None


def test_cli_wires_install_and_version(scratch, capsys):
    from recipe_engine import __version__
    from recipe_engine.cli import main
    assert main(["install", "--exe", r"C:\X\recipe-enginew.exe", "--no-autostart"]) == 0
    assert '"ok": true' in capsys.readouterr().out
    with pytest.raises(SystemExit) as ei:
        main(["--version"])
    assert ei.value.code == 0 and __version__ in capsys.readouterr().out
