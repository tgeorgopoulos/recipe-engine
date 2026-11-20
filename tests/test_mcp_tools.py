"""The MCP tools, called in-process through MCPServer.call_tool (no transport).
Covers the contract a client model relies on: structured replay outcomes, busy vs
queueing, refusals for destructive recipes and credential text, and that a paused
recording never saves."""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from recipe_engine import desktop, engine as engine_mod, recipes, safety
from recipe_engine.engine import Engine
from recipe_engine.mcp_server import build_server
from recipe_engine.screen import ActionResult


def call(server, _tool, **args):
    res = asyncio.run(server.call_tool(_tool, args))
    sc = getattr(res, "structured_content", None)
    if sc:
        # mcp 2.x wraps a plain str/list return as {"result": ...}; dict returns come through as-is
        return sc["result"] if set(sc) == {"result"} else sc
    texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
    try:
        return json.loads(texts[0]) if len(texts) == 1 else texts
    except (ValueError, IndexError):
        return texts


@pytest.fixture
def srv():
    return build_server(Engine())


def _browser_recipe(name):
    from types import SimpleNamespace
    url = "http://127.0.0.1:8765/"
    t = time.time()
    trace = [{"surface": "browser", "action": "goto", "selector": None, "value": url, "post": {"url": url, "title": "F"}, "t": t},
             {"surface": "browser", "action": "read", "selector": "role=table", "value": None, "post": {"url": url, "title": "F"}, "t": t + 1}]
    res = SimpleNamespace(login_blocked=False, login_seen=False, credential_flagged=False, cancelled=False,
                          error=None, hit_turn_limit=False, trace=trace)
    return recipes.capture(name, res, "browser", 2.0, destructive=False, description="fixture", name=name)


def test_tool_inventory_matches_the_spec(srv):
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert {"list_recipes", "record_recipe", "stop_recording", "replay_recipe", "recipe_stats", "forget_recipe",
            "schedule_recipe", "unschedule_recipe", "export_recipe", "import_recipe", "engine_status"} <= names
    assert {"screenshot", "click", "double_click", "right_click", "move", "type", "key", "scroll", "wait", "open_browser"} <= names
    assert {f"browser_{n}" for n in ("goto", "snapshot", "click", "fill", "press", "select", "check", "wait_for", "read", "screenshot", "scroll", "back", "download")} <= names
    assert len(names) == 34


def test_replay_of_unknown_recipe_is_structured_failed(srv):
    out = call(srv, "replay_recipe", name="nope")
    assert out["status"] == "failed" and out["failure_reason"].startswith("no_such_recipe")
    assert set(out) >= {"status", "seconds", "failed_step", "failure_reason", "screenshot_b64", "approaches_already_tried", "background_mode"}


def test_replay_ok_returns_what_it_read(srv, monkeypatch):
    r = _browser_recipe("mcp-ok")
    monkeypatch.setattr(recipes, "_replay_browser", lambda recipe, cancel, steps=None: (["Vendor Amount Acme Supplies 91.50"], len(recipe.steps)))
    out = call(srv, "replay_recipe", name="mcp-ok")
    assert out["status"] == "ok" and out["read_texts"] == ["Vendor Amount Acme Supplies 91.50"] and out["steps_done"] == 2
    lst = call(srv, "list_recipes")
    row = next(x for x in lst["recipes"] if x["name"] == "mcp-ok")
    assert row["status"] == "ready" and row["runs"] == 2
    assert "mcp-ok has run" in call(srv, "recipe_stats", name="mcp-ok")
    recipes.forget(r.id)


def test_second_replay_while_the_desktop_is_owned_is_busy_not_queued(srv, monkeypatch):
    r = _browser_recipe("mcp-busy")
    monkeypatch.setattr(recipes, "_replay_browser", lambda recipe, cancel, steps=None: ([], 2))
    held = threading.Event()
    release = threading.Event()

    def holder():
        with engine_mod.acquire_input("test-holder"):
            held.set()
            release.wait(5)
    t = threading.Thread(target=holder); t.start(); held.wait(2)
    try:
        assert call(srv, "engine_status")["input_owned"] is True
        out = call(srv, "replay_recipe", name="mcp-busy")
        assert out["status"] == "busy" and "test-holder" in out["failure_reason"]
    finally:
        release.set(); t.join()
    recipes.forget(r.id)


def test_failed_replay_reports_step_and_reason_and_the_history(srv, monkeypatch):
    r = _browser_recipe("mcp-fail")

    def boom(recipe, cancel, steps=None):
        raise recipes.ReplayFailed(2, "expect role=table not met: timeout", 1)
    monkeypatch.setattr(recipes, "_replay_browser", boom)
    out = call(srv, "replay_recipe", name="mcp-fail")
    assert out["status"] == "failed" and out["failed_step"] == 2 and "role=table" in out["failure_reason"] and out["steps_done"] == 1
    out2 = call(srv, "replay_recipe", name="mcp-fail")
    assert out2["status"] == "quarantined", "two consecutive failures quarantine"
    assert out2["approaches_already_tried"] and "step 2" in out2["approaches_already_tried"][0]
    recipes.forget(r.id)


def test_destructive_recording_is_refused_and_unknown_recipes_are_structured_failures(srv):
    out = call(srv, "record_recipe", name="send-report", description="email it")
    assert out["recording"] is False and "destructive" in out["reason"]
    assert "no_such_recipe" in call(srv, "schedule_recipe", name="x", cron="0 9 * * 1")["failure_reason"]
    assert "no_such_recipe" in call(srv, "export_recipe", name="x")["failure_reason"]


def test_type_with_a_credential_is_refused_and_pauses_recording(srv, monkeypatch):
    monkeypatch.setattr(desktop, "settle_and_shoot", lambda note: ActionResult(note=note))
    eng = srv._recipe_engine if hasattr(srv, "_recipe_engine") else None
    assert call(srv, "record_recipe", name="typing-run", description="")["recording"] is True
    out = call(srv, "type", text="my password is hunter2")
    assert any("refused" in str(x).lower() for x in out)
    stopped = call(srv, "stop_recording")
    assert stopped["saved"] is False and "paused" in stopped["reason"]


def test_engine_status_reports_invariants_and_refuses_when_broken(monkeypatch):
    eng = Engine()
    eng.invariant_problems = ["synthetic violation"]
    srv = build_server(eng)
    assert call(srv, "engine_status")["invariants_ok"] is False
    assert call(srv, "replay_recipe", name="anything")["status"] == "refused"
    assert call(srv, "record_recipe", name="anything", description="")["recording"] is False
