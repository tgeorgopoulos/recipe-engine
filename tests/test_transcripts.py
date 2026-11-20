"""Transcripts: one JSONL line per replay, and the plain-English reading of them."""
from __future__ import annotations

from types import SimpleNamespace

from recipe_engine import transcripts


def test_append_and_read_back_in_order():
    rid = "t1"
    transcripts.append(rid, {"name": "weekly-timesheet", "status": "ok", "seconds": 6.2})
    transcripts.append(rid, {"name": "weekly-timesheet", "status": "failed", "seconds": 3.0, "failed_step": 4,
                             "failure_reason": "expected window 'Timesheet - Chrome', foreground is 'Sign in'"})
    rows = transcripts.last(rid)
    assert [r["status"] for r in rows] == ["ok", "failed"] and all("t" in r for r in rows)


def test_failed_reasons_are_distinct_newest_first():
    rid = "t2"
    for i in range(3):
        transcripts.append(rid, {"status": "failed", "failed_step": 2, "failure_reason": "element not found"})
    transcripts.append(rid, {"status": "failed", "failed_step": 5, "failure_reason": "timeout"})
    transcripts.append(rid, {"status": "ok"})
    assert transcripts.failed_reasons(rid) == ["step 5: timeout", "step 2: element not found"]


def test_stats_text_reads_like_a_sentence():
    rid = "t3"
    for _ in range(13):
        transcripts.append(rid, {"status": "ok", "seconds": 6.0})
    transcripts.append(rid, {"status": "failed", "seconds": 2.0, "failed_step": 4, "failure_reason": "dialog moved"})
    recipe = SimpleNamespace(id=rid, name="weekly-timesheet", label="weekly timesheet", quarantined=False, last_success=None)
    text = transcripts.stats_text(recipe)
    assert text.startswith("weekly-timesheet has run 14 times in the last 30 days: 13 succeeded, 1 failed.")
    assert "step 4 - dialog moved" in text and "Typical successful run: 6.0 s" in text and "Not quarantined" in text


def test_stats_text_for_a_recipe_that_never_ran():
    recipe = SimpleNamespace(id="never", name="new-one", label="new one", quarantined=True, last_success=None)
    text = transcripts.stats_text(recipe)
    assert "has not run in the last 30 days" in text and "QUARANTINED" in text
