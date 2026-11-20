"""Export / import: signed JSON that carries the recipe and nothing about this
machine - no runs, history, cookies, storage or credential-looking text - and an
import that re-runs the safety checks and starts the recipe fresh."""
from __future__ import annotations

import json

import pytest

from recipe_engine import config, export, recipes


def _recipe(name="portal-invoices", steps=None, **kw):
    description = kw.pop("description", "download this month's invoices from the vendor portal")
    r = recipes.Recipe(id=recipes.recipe_id(name), command_normalized="download this month's invoices", name=name,
                       surface="browser", description=description,
                       runs={"success": 7, "fail": 1}, history=[{"ok": True, "t": 1.0, "s": 8.2}], last_success="2026-09-19T10:00:00+00:00",
                       consecutive_failures=1, app_hints=["Chrome"], params=[{"name": "month", "example": "2026-09", "used_in": [2]}],
                       steps=steps or [
                           {"surface": "browser", "action": "goto", "value": "https://portal.example/invoices", "expect": "role=link[name='Export']"},
                           {"surface": "browser", "action": "fill", "selector": "label=Month", "value": "{{month}}", "expect": "role=link[name='Export']"},
                           {"surface": "browser", "action": "download", "selector": "role=link[name='Export']", "expect": "url=https://portal.example/invoices"},
                       ], **kw)
    recipes.save(r)
    return r


@pytest.fixture
def key(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPORT_KEY_FILE", tmp_path / "export.key")
    yield
    for r in recipes.load_all():
        if r.name.startswith(("portal-", "imp-")):
            recipes.forget(r.id)


def test_export_carries_the_recipe_and_nothing_about_this_machine(key):
    _recipe()
    out = export.export_recipe("portal-invoices")
    assert out["status"] == "ok" and out["steps"] == 3, out
    payload = json.loads(out["json"])
    assert payload["format"] == "recipe-engine/1" and payload["signature"] and payload["key_id"] == export.key_id()
    rec = payload["recipe"]
    assert rec["name"] == "portal-invoices" and len(rec["steps"]) == 3 and rec["params"][0]["name"] == "month"
    for absent in ("runs", "history", "last_success", "last_used", "quarantined", "consecutive_failures", "imported", "id"):
        assert absent not in rec, absent
    low = out["json"].lower()
    for word in ("cookie", "storage", "token", "sid="):
        assert word not in low, word
    assert (config.EXPORT_KEY_FILE).read_bytes()[:8] in (b"REDPAPI1", b"REPLAIN1")


def test_round_trip_imports_a_fresh_unquarantined_copy(key):
    r = _recipe()
    text = export.export_recipe("portal-invoices")["json"]
    recipes.forget(r.id)
    out = export.import_recipe(text)
    assert out["status"] == "ok" and out["signature_ok"] is True and out["steps"] == 3, out
    back = recipes.load_by_name("portal-invoices")
    assert back.steps == r.steps and back.params == r.params and back.description == r.description
    assert back.runs == {"success": 0, "fail": 0} and back.history == [] and back.quarantined is False
    assert back.imported is True and back.consecutive_failures == 0 and back.last_success is None


def test_a_foreign_or_edited_signature_imports_with_signature_ok_false(key, monkeypatch):
    _recipe()
    text = export.export_recipe("portal-invoices")["json"]
    recipes.forget(recipes.recipe_id("portal-invoices"))
    tampered = json.loads(text)
    tampered["recipe"]["description"] = "edited on the way"
    out = export.import_recipe(json.dumps(tampered))
    assert out["status"] == "ok" and out["signature_ok"] is False and "another install" in out["note"]
    assert recipes.load_by_name("portal-invoices").description == "edited on the way"
    recipes.forget(recipes.recipe_id("portal-invoices"))
    # the same file on a machine with a different key
    monkeypatch.setattr(export, "export_key", lambda: b"\x01" * 32)
    out = export.import_recipe(text)
    assert out["status"] == "ok" and out["signature_ok"] is False


def test_secrets_never_leave_and_never_enter(key):
    _recipe("portal-secret", steps=[{"surface": "browser", "action": "fill", "selector": "label=Password", "value": "x", "expect": "url=https://x/"}])
    out = export.export_recipe("portal-secret")
    assert out["status"] == "refused" and "credential" in out["failure_reason"]
    _recipe()
    text = export.export_recipe("portal-invoices")["json"]
    recipes.forget(recipes.recipe_id("portal-invoices"))
    p = json.loads(text)
    p["recipe"]["steps"][1]["value"] = "the OTP is 123456"
    assert "credential" in export.import_recipe(json.dumps(p))["failure_reason"]
    p = json.loads(text)
    p["recipe"]["cookies"] = [{"name": "sid", "value": "abc"}]
    out = export.import_recipe(json.dumps(p))
    assert out["status"] == "refused" and "forbidden keys" in out["failure_reason"] and "cookies" in out["failure_reason"]
    assert recipes.load_by_name("portal-invoices") is None


def test_destructive_recipes_are_refused_both_ways(key):
    _recipe("portal-pay", description="pay the invoice")
    assert export.export_recipe("portal-pay")["status"] == "refused"
    _recipe()
    text = export.export_recipe("portal-invoices")["json"]
    recipes.forget(recipes.recipe_id("portal-invoices"))
    p = json.loads(text)
    p["recipe"]["steps"].append({"surface": "browser", "action": "click", "selector": "role=button[name='Submit']", "expect": "url=https://x/"})
    out = export.import_recipe(json.dumps(p))
    assert out["status"] == "refused" and "submit" in out["failure_reason"].lower()
    assert recipes.load_by_name("portal-invoices") is None


def test_import_validates_shape_name_and_collisions(key):
    assert export.import_recipe("{not json")["status"] == "failed"
    assert export.import_recipe(json.dumps({"format": "other"}))["status"] == "failed"
    _recipe()
    text = export.export_recipe("portal-invoices")["json"]
    assert "already exists" in export.import_recipe(text)["failure_reason"]
    assert export.import_recipe(text, replace=True)["status"] == "ok"
    p = json.loads(text)
    p["recipe"]["name"] = "Bad Name!"
    assert "bad recipe name" in export.import_recipe(json.dumps(p))["failure_reason"]
    p = json.loads(text)
    p["recipe"]["steps"] = []
    assert "no usable steps" in export.import_recipe(json.dumps(p))["failure_reason"]


def test_mcp_tools_export_and_import(key):
    import asyncio
    from recipe_engine.engine import Engine
    from recipe_engine.mcp_server import build_server
    _recipe()
    srv = build_server(Engine())

    def call(tool, **args):
        res = asyncio.run(srv.call_tool(tool, args))
        sc = getattr(res, "structured_content", None)
        if sc:
            return sc["result"] if set(sc) == {"result"} else sc
        return json.loads([c.text for c in res.content if getattr(c, "type", "") == "text"][0])
    out = call("export_recipe", name="portal-invoices")
    assert out["status"] == "ok" and '"format": "recipe-engine/1"' in out["json"]
    recipes.forget(recipes.recipe_id("portal-invoices"))
    imp = call("import_recipe", json=out["json"])
    assert imp["status"] == "ok" and imp["signature_ok"] is True
