"""The headline claim: a replay makes ZERO model calls.

Three independent proofs:
1. a meta-path finder that raises if any banned SDK is imported during a replay,
2. socket name resolution monkeypatched to raise for anthropic/claude hosts,
3. a static AST scan of the whole package for the banned imports.
"""
from __future__ import annotations

import ast
import importlib.abc
import socket
import sys
import time
from pathlib import Path

import pytest

from recipe_engine import config, engine, recipes

BANNED = ("claude_agent_sdk", "anthropic", "openai")


class _Blocker(importlib.abc.MetaPathFinder):
    def __init__(self):
        self.attempts: list[str] = []

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in BANNED:
            self.attempts.append(fullname)
            raise ImportError(f"blocked by test: {fullname}")
        return None


def _fake_browser_recipe(name: str) -> recipes.Recipe:
    from types import SimpleNamespace
    url = "http://127.0.0.1:8765/"
    t = time.time()
    trace = [
        {"surface": "browser", "action": "goto", "selector": None, "value": url, "post": {"url": url, "title": "F"}, "t": t},
        {"surface": "browser", "action": "read", "selector": "role=table", "value": None, "post": {"url": url, "title": "F"}, "t": t + 1},
    ]
    res = SimpleNamespace(login_blocked=False, login_seen=False, credential_flagged=False, cancelled=False,
                          error=None, hit_turn_limit=False, trace=trace)
    r = recipes.capture(name, res, "browser", 2.0, destructive=False, description="fixture", name=name)
    assert r is not None
    return r


@pytest.fixture
def blocked(monkeypatch):
    b = _Blocker()
    sys.meta_path.insert(0, b)
    real = socket.getaddrinfo

    def guarded(host, *a, **k):
        if any(x in str(host).lower() for x in ("anthropic", "claude")):
            raise AssertionError(f"network to a model host during replay: {host}")
        return real(host, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", guarded)
    yield b
    sys.meta_path.remove(b)


def test_replay_imports_no_model_sdk_and_touches_no_model_host(blocked, monkeypatch):
    r = _fake_browser_recipe("zero-token-check")
    monkeypatch.setattr(recipes, "_replay_browser", lambda recipe, cancel, steps=None: (["Mathematics 91"], len(recipe.steps)))
    out = engine.Engine().replay("zero-token-check")
    assert out["status"] == "ok" and out["read_texts"] == ["Mathematics 91"], out
    assert blocked.attempts == [], f"replay tried to import {blocked.attempts}"
    recipes.forget(r.id)


def test_no_module_in_the_package_imports_a_model_sdk():
    hits = []
    for f in sorted(Path(config.PACKAGE_DIR).glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if n.split(".")[0] in BANNED:
                    hits.append(f"{f.name}: {n}")
    assert hits == [], hits


def test_the_installed_environment_has_no_model_sdk_either():
    """Not a package property but a deployment one: the exe we ship must not carry it."""
    import importlib.util
    assert importlib.util.find_spec("openai") is None
