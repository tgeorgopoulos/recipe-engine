"""Schema v2: migration of the three checked-in v1 recipes, round-trip, {{params}}."""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from recipe_engine import config, recipes, safety

FIX = Path(__file__).parent / "fixtures" / "recipes_v1"


@pytest.mark.parametrize("fname", sorted(p.name for p in FIX.glob("*.json")))
def test_v1_recipes_migrate_in_memory(fname):
    d = json.loads((FIX / fname).read_text(encoding="utf-8"))
    assert int(d.get("schema", 1)) == 1
    r = recipes.Recipe.from_dict(d)
    assert r.schema == 2 and r.name and r.params == [] and r.background_capable is False
    for st in r.steps:
        assert "params" in st and "expect_timeout_ms" in st
        if r.surface == "desktop":
            assert st["input_kind"] == "input" and st["uia"] is None and "rel" not in st
            if st["pixel"] is not None:
                assert st["pixel"]["display"] == r.display and len(st["pixel"]["rel"]) == 2
    # a v1 desktop recipe stays pixel-only and display-locked: that is the honest outcome
    if r.surface == "desktop":
        assert all(st["pixel"] is not None for st in r.steps if st["action"] in ("click", "double_click", "right_click", "scroll"))


def test_v2_round_trip_through_disk():
    d = json.loads((FIX / "71beae0d26a3.json").read_text(encoding="utf-8"))
    r = recipes.Recipe.from_dict(d)
    recipes.save(r)
    back = recipes.load(r.id)
    assert back is not None and back.schema == 2 and back.steps == r.steps and back.name == r.name
    recipes.forget(r.id)


def _trace_with(value):
    url = "http://127.0.0.1:8765/"
    t = time.time()
    return [{"surface": "browser", "action": "goto", "selector": None, "value": url, "post": {"url": url, "title": "F"}, "t": t},
            {"surface": "browser", "action": "fill", "selector": "label=Search", "value": value, "post": {"url": url, "title": "F"}, "t": t + 1},
            {"surface": "browser", "action": "read", "selector": "role=table", "value": None, "post": {"url": url, "title": "F"}, "t": t + 2}]


def _res(trace):
    return SimpleNamespace(login_blocked=False, login_seen=False, credential_flagged=False, cancelled=False,
                           error=None, hit_turn_limit=False, trace=trace)


def test_placeholders_become_params_and_substitute_validates():
    r = recipes.capture("params-run", _res(_trace_with("hours for {{week}}")), "browser", 3.0, destructive=False,
                        name="params-run", params=[{"name": "week", "example": "week 12"}])
    assert r is not None
    assert [p["name"] for p in r.params] == ["week"] and r.params[0]["used_in"] == [2] and r.params[0]["example"] == "week 12"
    assert r.steps[1]["params"] == ["week"]
    with pytest.raises(recipes.MissingParam) as ei:
        recipes.substitute(r, {})
    assert ei.value.names == ["week"]
    steps = recipes.substitute(r, {"week": "38", "ignored": "x"})
    assert steps[1]["value"] == "hours for 38" and r.steps[1]["value"] == "hours for {{week}}", "the recipe itself is untouched"
    with pytest.raises(safety.CredentialRefused):
        recipes.substitute(r, {"week": "my password is hunter2"})
    recipes.forget(r.id)


def test_pattern_kind_and_background_capable():
    invoke = {"automation_id": "btn", "name": "OK", "control_type": "ButtonControl", "class_name": "Button", "ancestors": [], "pattern": "invoke", "is_password": False}
    edit = {**invoke, "pattern": "set_value", "control_type": "EditControl"}
    pw = {**edit, "is_password": True}
    assert recipes._pattern_kind("click", invoke) == "pattern"
    assert recipes._pattern_kind("type", edit) == "pattern"
    assert recipes._pattern_kind("type", pw) == "input", "a password field is never pattern-driven"
    assert recipes._pattern_kind("key", None) == "input"
    t = time.time()
    trace = [{"surface": "desktop", "action": "click", "value": None, "rel": [0.5, 0.5], "uia": invoke, "post": {"window": "App"}, "t": t}]
    r = recipes.build("bg", trace, "desktop", 1.0, display={"w": 1920, "h": 1080, "scale": 1.0}, name="bg")
    assert r.steps[0]["input_kind"] == "pattern" and r.steps[0]["uia"] == invoke and r.steps[0]["pixel"]["rel"] == [0.5, 0.5]
    assert r.background_capable is True
    trace.append({"surface": "desktop", "action": "key", "value": "ctrl+s", "post": {"window": "App"}, "t": t + 1})
    assert recipes.build("bg2", trace, "desktop", 1.0, display={"w": 1920, "h": 1080, "scale": 1.0}).background_capable is False


def test_engine_refuses_missing_inputs_before_touching_the_desktop(monkeypatch):
    from recipe_engine import engine as engine_mod
    r = recipes.capture("needs-week", _res(_trace_with("{{week}}")), "browser", 1.0, destructive=False, name="needs-week")
    called = []
    monkeypatch.setattr(recipes, "_replay_browser", lambda *a, **k: called.append(1) or ([], 3))
    out = engine_mod.Engine().replay("needs-week")
    assert out["status"] == "refused" and "missing inputs: week" in out["failure_reason"] and called == []
    out = engine_mod.Engine().replay("needs-week", {"week": "38"})
    assert out["status"] == "ok" and called == [1]
    recipes.forget(r.id)
