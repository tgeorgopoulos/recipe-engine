"""Record -> replay on a real Notepad through the real engine path, then the UI
mutation case: the recorded window is gone, so the replay fails with an honest
reason and two failures in a row quarantine the recipe. (-m integration; the 50x
numbers come from tests/reliability_study.py.)"""
from __future__ import annotations

import sys

import pytest

from recipe_engine import config, recipes
from recipe_engine.engine import Engine

pytestmark = [pytest.mark.integration, pytest.mark.skipif(sys.platform != "win32", reason="Windows only")]


@pytest.fixture
def study():
    sys.path.insert(0, str(config.PROJECT_DIR))
    from recipe_engine import display
    display.make_dpi_aware()
    import tests.reliability_study as rs
    yield rs
    r = recipes.load_by_name("live-notepad")
    if r is not None:
        recipes.forget(r.id)


def test_record_then_replay_three_times_then_quarantine_when_the_window_is_gone(study):
    recipe = study.record_notepad("live-notepad")
    assert recipe.surface == "desktop" and [s["action"] for s in recipe.steps] == ["click", "type"]
    assert recipe.steps[0].get("uia") and recipe.steps[0]["uia"]["ancestors"][0]["class_name"] == "Notepad"
    eng = Engine()
    for i in range(1, 4):
        rr = study.replay_notepad(eng, "live-notepad", i)
        assert rr.status == "ok" and rr.verified, rr
        assert rr.seconds < 10, rr
    # UI mutation: no Notepad window with the recorded title exists -> unresolvable, twice -> quarantined
    out1 = eng.replay("live-notepad")
    assert out1["status"] == "failed" and out1["failed_step"] == 1, out1
    assert "unresolvable_element" in out1["failure_reason"] or "window" in out1["failure_reason"], out1
    out2 = eng.replay("live-notepad")
    assert out2["status"] == "quarantined", out2
    assert out2["approaches_already_tried"], "the second failure must carry the first reason"
    out3 = eng.replay("live-notepad")
    assert out3["status"] == "quarantined" and "re-record" in out3["failure_reason"]
