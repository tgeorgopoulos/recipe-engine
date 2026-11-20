"""Packaging gates (-m packaging, run in CI): the .mcpb manifest validates, the
Claude Code plugin validates under --strict, and the extension has no Python and
no bin/ (Claude Desktop bundles Node, the host is installed separately)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.packaging


def test_mcpb_manifest_validates():
    npx = shutil.which("npx")
    if npx is None:
        pytest.skip("npx not installed")
    r = subprocess.run([npx, "--yes", "@anthropic-ai/mcpb", "validate", "manifest.json"], cwd=ROOT / "mcpb", capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr


def test_claude_plugin_validates_strict():
    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("claude CLI not installed")
    r = subprocess.run([claude, "plugin", "validate", str(ROOT), "--strict"], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_extension_is_a_node_shim_with_no_python_and_no_bin():
    m = json.loads((ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    assert m["server"]["type"] == "node" and m["server"]["entry_point"] == "server/shim.js"
    assert (ROOT / "mcpb" / "server" / "shim.js").exists()
    assert not (ROOT / "bin").exists() and not (ROOT / "mcpb" / "bin").exists()
    assert not list((ROOT / "mcpb").rglob("*.py"))
    shim = (ROOT / "mcpb" / "server" / "shim.js").read_text(encoding="utf-8")
    assert "require(" in shim and "node_modules" not in shim, "the shim must stay dependency-free"


def test_plugin_files_reference_each_other_and_pin_sha256_verification():
    p = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert p["name"] == "recipe-engine" and p["mcpServers"] == "./.mcp.json" and p["hooks"] == "./hooks/hooks.json"
    mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    srv = mcp["mcpServers"]["recipe-engine"]
    assert srv["type"] == "http" and srv["headers"]["Authorization"].startswith("Bearer ${")
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    cmd = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "install-host.ps1" in cmd and "${CLAUDE_PLUGIN_ROOT}" in cmd
    ps1 = (ROOT / "scripts" / "install-host.ps1").read_text(encoding="utf-8")
    assert "Get-FileHash" in ps1 and "SHA256" in ps1 and "mismatch" in ps1 and "Authenticode" not in ps1
