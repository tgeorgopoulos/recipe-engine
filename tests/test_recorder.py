"""The recorder: arms on record_recipe, drops destructive steps as they happen, pauses
on a login page / refused credential, and hands the trace to recipes.capture."""
from __future__ import annotations

import time

from recipe_engine import desktop, recipes
from recipe_engine.recorder import Recorder

URL = "http://127.0.0.1:8765/"


def _browser_trace():
    t = time.time()
    return [
        {"surface": "browser", "action": "goto", "selector": None, "value": URL, "post": {"url": URL, "title": "Fixture"}, "t": t},
        {"surface": "browser", "action": "click", "selector": "role=link[name='Invoices']", "value": None,
         "post": {"url": URL + "invoices.html", "title": "Invoices"}, "t": t + 1.2},
        {"surface": "browser", "action": "read", "selector": "role=table", "value": None,
         "post": {"url": URL + "invoices.html", "title": "Invoices"}, "t": t + 2.0},
    ]


def test_record_stop_saves_a_recipe_by_name():
    rec = Recorder()
    ok, why = rec.start("fixture-invoices", "read the invoices table", ["Chrome"])
    assert ok, why
    assert desktop._recorder_hook == rec.record, "desktop actions must flow into the recorder"
    for step in _browser_trace():
        rec.record(step)
    r, why = rec.stop()
    assert r is not None, why
    assert r.name == "fixture-invoices" and r.surface == "browser" and len(r.steps) == 3
    assert r.app_hints == ["Chrome"] and r.description == "read the invoices table"
    assert recipes.load_by_name("fixture-invoices") is r or recipes.load_by_name("fixture-invoices").id == r.id
    assert desktop._recorder_hook is None
    recipes.forget(r.id)


def test_names_are_validated_and_destructive_names_refused():
    rec = Recorder()
    assert not rec.start("Weekly Timesheet", "")[0]
    assert not rec.start("x", "")[0]
    ok, why = rec.start("send-invoice", "email the invoice to accounting")
    assert not ok and "destructive" in why


def test_destructive_steps_are_dropped_as_they_happen():
    rec = Recorder()
    assert rec.start("safe-run", "")[0]
    rec.record(_browser_trace()[0])
    rec.record({"surface": "browser", "action": "click", "selector": "role=button[name='Send']", "value": None,
                "post": {"url": URL, "title": "x"}, "t": time.time()})
    assert len(rec.trace) == 1 and "send" in rec.destructive_hit.lower()
    r, why = rec.stop()
    # the remaining goto alone is a recordable step, so a recipe IS saved - without the
    # Send click, and the caller is told a destructive step was dropped
    assert r is not None and len(r.steps) == 1 and r.steps[0]["action"] == "goto"
    assert "destructive step was dropped" in why
    recipes.forget(r.id)


def test_login_page_pauses_and_nothing_is_saved():
    rec = Recorder()
    assert rec.start("needs-login", "")[0]
    rec.record(_browser_trace()[0])
    rec.pause("login page seen")
    rec.record(_browser_trace()[1])                      # ignored while paused
    assert len(rec.trace) == 1 and rec.state == "paused"
    r, why = rec.stop()
    assert r is None and "paused" in why and recipes.load_by_name("needs-login") is None


def test_refused_credential_pauses_recording():
    rec = Recorder()
    assert rec.start("cred-run", "")[0]
    rec.pause("credential refused")
    assert rec.credential_flagged and rec.state == "paused"
    assert rec.stop()[0] is None


def test_duplicate_name_is_refused_until_forgotten():
    rec = Recorder()
    assert rec.start("dupe", "")[0]
    for step in _browser_trace():
        rec.record(step)
    r, _ = rec.stop()
    assert r is not None
    ok, why = Recorder().start("dupe", "")
    assert not ok and "already exists" in why
    recipes.forget(r.id)
    assert Recorder().start("dupe", "")[0]
