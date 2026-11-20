"""Scheduling: the cron parser against a table of known next-runs, the job file
round-trip, the refusal rules, and a fake-clock scheduler that runs due jobs
through a fake engine, defers on busy, and records the outcome."""
from __future__ import annotations

from datetime import datetime

import pytest

from recipe_engine import config, recipes, scheduler
from recipe_engine.scheduler import CronError, Job, Scheduler, parse_cron

T0 = datetime(2026, 9, 20, 10, 30)          # a Sunday


@pytest.mark.parametrize("expr,after,want", [
    ("* * * * *", T0, datetime(2026, 9, 20, 10, 31)),
    ("0 9 * * 1", T0, datetime(2026, 9, 21, 9, 0)),           # Mondays 09:00 -> tomorrow
    ("0 9 * * 1", datetime(2026, 9, 21, 9, 0), datetime(2026, 9, 28, 9, 0)),   # strictly after
    ("30 17 * * 1-5", datetime(2026, 9, 18, 17, 30), datetime(2026, 9, 21, 17, 30)),  # Fri 17:30 -> Mon
    ("*/15 * * * *", T0, datetime(2026, 9, 20, 10, 45)),
    ("0 0 1 * *", T0, datetime(2026, 10, 1, 0, 0)),
    ("0 12 29 2 *", T0, datetime(2028, 2, 29, 12, 0)),          # skips whole months and years
    ("0 8 15 * 1", datetime(2026, 9, 14, 0, 0), datetime(2026, 9, 14, 8, 0)),  # dom OR dow (Vixie): Monday 14th matches by weekday
    ("0 8 15 * 1", datetime(2026, 9, 14, 9, 0), datetime(2026, 9, 15, 8, 0)),  # ... then the 15th by day-of-month
    ("5 4 * * 7", T0, datetime(2026, 9, 27, 4, 5)),            # 7 == Sunday
    ("@daily", T0, datetime(2026, 9, 21, 9, 0)),
    ("0 9,17 * * *", T0, datetime(2026, 9, 20, 17, 0)),
])
def test_next_after_table(expr, after, want):
    assert parse_cron(expr).next_after(after) == want


@pytest.mark.parametrize("bad", ["", "0 9 * *", "60 * * * *", "0 25 * * *", "0 9 32 * *", "0 9 * 13 *", "0 9 * * 8",
                                 "a b c d e", "*/0 * * * *", "5-1 * * * *", "0 9 * * mon"])
def test_bad_expressions_are_refused_with_a_reason(bad):
    with pytest.raises(CronError):
        parse_cron(bad)


def test_never_matching_expression_is_refused_not_looped():
    with pytest.raises(CronError):
        parse_cron("0 0 31 2 *").next_after(T0)                 # Feb 31 never comes


# ------------------------------------------------------------- jobs + rules
def _recipe(name="weekly-report", steps=None, quarantined=False, description="export the weekly report"):
    r = recipes.Recipe(id=recipes.recipe_id(name), command_normalized=name.replace("-", " "), name=name, surface="browser",
                       description=description, quarantined=quarantined,
                       steps=steps or [{"surface": "browser", "action": "goto", "value": "http://127.0.0.1:8765/", "expect": "url=http://127.0.0.1:8765/"}])
    recipes.save(r)
    return r


@pytest.fixture
def clean_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_FILE", tmp_path / "jobs.json")
    yield
    for r in recipes.load_all():
        if r.name.startswith(("weekly-report", "sched-")):
            recipes.forget(r.id)


def test_add_list_remove_round_trip_through_the_jobs_file(clean_jobs):
    _recipe()
    out = scheduler.add_job("weekly-report", "0 9 * * 1", {}, now=T0)
    assert out["status"] == "ok" and out["next_run"] == "2026-09-21 09:00:00", out
    jobs = scheduler.list_jobs()
    assert len(jobs) == 1 and jobs[0].recipe == "weekly-report" and jobs[0].cron == "0 9 * * 1"
    assert config.JOBS_FILE.exists() and config.JOBS_FILE.read_bytes()[:8] in (b"REDPAPI1", b"REPLAIN1")
    # re-scheduling replaces, never duplicates
    scheduler.add_job("weekly-report", "0 8 * * 1", {}, now=T0)
    assert [j.cron for j in scheduler.list_jobs()] == ["0 8 * * 1"]
    assert scheduler.remove_job("weekly-report")["status"] == "ok"
    assert scheduler.list_jobs() == []
    assert scheduler.remove_job("weekly-report")["status"] == "failed"


def test_refusals_unknown_quarantined_destructive_missing_param_bad_cron(clean_jobs):
    assert "no_such_recipe" in scheduler.add_job("sched-nope", "0 9 * * 1")["failure_reason"]
    _recipe("sched-q", quarantined=True)
    assert "quarantined" in scheduler.add_job("sched-q", "0 9 * * 1")["failure_reason"]
    _recipe("sched-d", description="submit the timesheet")
    assert "refused" in scheduler.add_job("sched-d", "0 9 * * 1")["failure_reason"]
    _recipe("sched-p", steps=[{"surface": "browser", "action": "fill", "selector": "label=Week", "value": "{{week}}", "expect": "url=http://x/"}])
    out = scheduler.add_job("sched-p", "0 9 * * 1")
    assert out["status"] == "refused" and "week" in out["failure_reason"]
    assert scheduler.add_job("sched-p", "0 9 * * 1", {"week": "12"})["status"] == "ok"
    assert "credential" in scheduler.add_job("sched-p", "0 9 * * 1", {"week": "my password is x"})["failure_reason"]
    assert "bad_schedule" in scheduler.add_job("sched-p", "every monday", {"week": "1"})["failure_reason"]
    assert scheduler.list_jobs()[0].inputs == {"week": "12"}


# ------------------------------------------------------------- the runner
class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_fake_clock_scheduler_runs_due_jobs_records_outcomes_and_defers_on_busy(clean_jobs):
    _recipe()
    scheduler.add_job("weekly-report", "0 9 * * 1", {}, now=T0)
    clock = _Clock(T0)
    calls = []
    answers = iter([{"status": "ok", "seconds": 3.2}, {"status": "busy", "failure_reason": "the desktop is owned by a replay"},
                    {"status": "failed", "seconds": 1.0, "failure_reason": "step 1: expect not met"}])
    s = Scheduler(engine=None, clock=clock, replay=lambda name, inputs: calls.append((name, inputs)) or next(answers))

    assert s.tick() == [] and calls == []                       # 10:30 Sunday: nothing due
    clock.t = datetime(2026, 9, 21, 9, 0, 30)                   # Monday 09:00:30 -> due
    out = s.tick()
    assert out and out[0]["status"] == "ok" and calls == [("weekly-report", {})]
    j = scheduler.list_jobs()[0]
    assert j.next_run == "2026-09-28 09:00:00" and j.runs == 1 and j.last["status"] == "ok" and j.last["seconds"] == 3.2
    assert s.tick() == [], "ran twice in the same minute"

    clock.t = datetime(2026, 9, 28, 9, 0, 5)                    # next Monday: the desktop is busy
    out = s.tick()
    assert out[0]["status"] == "busy"
    j = scheduler.list_jobs()[0]
    assert j.next_run == "2026-09-28 09:05:05", "a busy desktop defers 5 minutes, it does not queue"
    clock.t = datetime(2026, 9, 28, 9, 5, 10)
    out = s.tick()
    assert out[0]["status"] == "failed"
    j = scheduler.list_jobs()[0]
    assert j.next_run == "2026-10-05 09:00:00" and j.last["failure_reason"] == "step 1: expect not met" and j.runs == 3


def test_a_recipe_that_became_quarantined_after_scheduling_is_refused_at_run_time(clean_jobs):
    r = _recipe()
    scheduler.add_job("weekly-report", "* * * * *", {}, now=T0)
    r.quarantined = True
    recipes.save(r)
    s = Scheduler(engine=None, clock=_Clock(datetime(2026, 9, 20, 10, 31, 1)), replay=lambda n, i: pytest.fail("replayed a quarantined recipe"))
    out = s.tick()
    assert out[0]["status"] == "refused" and "quarantined" in out[0]["failure_reason"]


def test_mcp_tools_schedule_and_unschedule(clean_jobs):
    import asyncio
    from recipe_engine.engine import Engine
    from recipe_engine.mcp_server import build_server
    _recipe()
    srv = build_server(Engine())

    def call(tool, **args):
        import json
        res = asyncio.run(srv.call_tool(tool, args))
        sc = getattr(res, "structured_content", None)
        if sc:
            return sc["result"] if set(sc) == {"result"} else sc
        return json.loads([c.text for c in res.content if getattr(c, "type", "") == "text"][0])
    out = call("schedule_recipe", name="weekly-report", cron="@weekly")
    assert out["status"] == "ok" and out["cron"] == "0 9 * * 1", out
    assert call("engine_status")["jobs"] == 1
    assert call("unschedule_recipe", name="weekly-report")["status"] == "ok"
    assert call("engine_status")["jobs"] == 0


def test_job_file_round_trips_inputs_and_history(clean_jobs):
    j = Job(recipe="x", cron="0 9 * * 1", inputs={"week": "12"}, created="2026-09-20 10:30:00", next_run="2026-09-21 09:00:00",
            last={"at": "2026-09-14 09:00:00", "status": "ok", "seconds": 4.1, "failure_reason": ""}, runs=3)
    scheduler._save([j])
    back = scheduler.list_jobs()[0]
    assert back == j
