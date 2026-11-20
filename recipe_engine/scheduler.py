"""Scheduled replays: a 5-field cron parser (no dependency), a jobs file, and a
thread that runs due jobs through Engine.replay like any other client.

    minute hour day-of-month month day-of-week      local time
    *  ,  -  /   are supported; day-of-week 0-7 (0 and 7 are Sunday); when both
    day fields are restricted either one matching fires the job (Vixie cron).

Rules (the same ones a client faces, enforced at schedule time and again at run
time): the recipe must exist, not be quarantined, not be destructive, and every
{{parameter}} must be supplied in `inputs` (a credential-looking input is
refused). If the desktop is busy when a job comes due, it is deferred 5 minutes,
not queued. Every run is a transcript entry with trigger="schedule".
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from recipe_engine import config, recipes, safety, sealed
from recipe_engine.config import log

DEFER_BUSY_S = 300
TICK_S = 15
_lock = threading.RLock()


# ---------------------------------------------------------------- cron
class CronError(ValueError):
    pass


_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_NAMES = {"minute": 0, "hour": 1, "day": 2, "month": 3, "weekday": 4}
_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 9 * * *", "@weekly": "0 9 * * 1", "@monthly": "0 9 1 * *"}


def _field(spec: str, lo: int, hi: int, name: str) -> tuple[set[int], bool]:
    """-> (allowed values, restricted?)  restricted is False for a bare '*'."""
    out: set[int] = set()
    restricted = True
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"{name}: empty list item")
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise CronError(f"{name}: bad step {s!r}")
            step = int(s)
        if part == "*":
            a, b = lo, hi
            if step == 1:
                restricted = False
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            if not (a_s.isdigit() and b_s.isdigit()):
                raise CronError(f"{name}: bad range {part!r}")
            a, b = int(a_s), int(b_s)
        elif part.isdigit():
            a = b = int(part)
        else:
            raise CronError(f"{name}: {part!r} is not a number, range, list or *")
        if a < lo or b > hi or a > b:
            raise CronError(f"{name}: {part!r} outside {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out, restricted


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: frozenset
    hours: frozenset
    days: frozenset
    months: frozenset
    weekdays: frozenset
    day_restricted: bool
    weekday_restricted: bool

    def matches(self, t: datetime) -> bool:
        return t.minute in self.minutes and t.hour in self.hours and self.day_matches(t)

    def day_matches(self, t: datetime) -> bool:
        if t.month not in self.months:
            return False
        dom = t.day in self.days
        dow = ((t.weekday() + 1) % 7) in self.weekdays          # python Monday=0 -> cron Sunday=0
        if self.day_restricted and self.weekday_restricted:
            return dom or dow
        if self.day_restricted:
            return dom
        if self.weekday_restricted:
            return dow
        return True

    def next_after(self, t: datetime) -> datetime:
        """First matching minute strictly after `t`. Skips whole months/days/hours that
        cannot match, so even 'Feb 29' resolves in milliseconds."""
        cur = (t.replace(second=0, microsecond=0) + timedelta(minutes=1))
        limit = cur + timedelta(days=366 * 5)
        while cur < limit:
            if cur.month not in self.months:
                nm = (cur.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0)
                cur = nm
                continue
            if not self.day_matches(cur):
                cur = (cur + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if cur.hour not in self.hours:
                cur = (cur + timedelta(hours=1)).replace(minute=0)
                continue
            if cur.minute not in self.minutes:
                cur = cur + timedelta(minutes=1)
                continue
            return cur
        raise CronError(f"{self.expr!r} never matches in the next five years")

    def describe(self) -> str:
        return self.expr


def parse_cron(expr: str) -> Cron:
    raw = (expr or "").strip()
    raw = _ALIASES.get(raw.lower(), raw)
    parts = raw.split()
    if len(parts) != 5:
        raise CronError("a schedule has five fields: minute hour day month weekday (e.g. '0 9 * * 1' = Mondays 09:00)")
    vals = []
    flags = []
    for spec, (lo, hi), name in zip(parts, _RANGES, _NAMES):
        v, restricted = _field(spec, lo, hi, name)
        if name == "weekday":
            v = {0 if x == 7 else x for x in v}
        vals.append(frozenset(v))
        flags.append(restricted)
    return Cron(raw, vals[0], vals[1], vals[2], vals[3], vals[4], flags[2], flags[4])


# ---------------------------------------------------------------- jobs
@dataclass
class Job:
    recipe: str
    cron: str
    inputs: dict[str, str] = field(default_factory=dict)
    created: str = ""
    next_run: str = ""
    last: dict[str, Any] = field(default_factory=dict)
    runs: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job":
        known = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _path() -> Path:
    return Path(config.JOBS_FILE)


def _load() -> list[Job]:
    p = _path()
    if not p.exists():
        return []
    try:
        data = sealed.read_sealed_json(p)
        return [Job.from_dict(j) for j in (data.get("jobs") if isinstance(data, dict) else data) or []]
    except Exception as e:
        log("SCHEDULE", f"jobs file unreadable ({e!r}); starting empty")
        return []


def _save(jobs: list[Job]) -> None:
    sealed.write_sealed_json(_path(), {"version": 1, "jobs": [j.to_dict() for j in jobs]})


def list_jobs() -> list[Job]:
    with _lock:
        return _load()


def _iso(t: datetime) -> str:
    return t.replace(microsecond=0).isoformat(sep=" ")


def _check(name: str, inputs: dict[str, str]) -> tuple[Optional[recipes.Recipe], str]:
    """The refusal rules, shared by schedule time and run time."""
    r = recipes.load_by_name(name)
    if r is None:
        return None, f"no_such_recipe: {name!r}"
    if r.quarantined:
        return None, "quarantined: re-record the recipe before scheduling it"
    text = " ".join([r.name, r.description] + [str(s.get("value") or "") + " " + str(s.get("selector") or "") + " "
                                              + str((s.get("uia") or {}).get("name") or "") for s in r.steps])
    if safety.is_destructive(text):
        return None, f"refused: {safety.destructive_reason(text)} - the engine never sends, submits, buys or deletes"
    try:
        recipes.substitute(r, inputs)
    except recipes.MissingParam as e:
        return None, f"refused: {e}. Pass them in `inputs`."
    except safety.CredentialRefused as e:
        return None, f"refused: an input looks like a credential ({e})"
    return r, ""


def add_job(name: str, cron: str, inputs: Optional[dict[str, str]] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    inputs = {str(k): str(v) for k, v in (inputs or {}).items()}
    try:
        c = parse_cron(cron)
    except CronError as e:
        return {"status": "refused", "failure_reason": f"bad_schedule: {e}"}
    r, why = _check(name, inputs)
    if r is None:
        return {"status": "refused", "failure_reason": why}
    nxt = c.next_after(now or datetime.now())
    with _lock:
        jobs = [j for j in _load() if j.recipe != r.name]
        job = Job(recipe=r.name, cron=c.expr, inputs=inputs, created=_iso(now or datetime.now()), next_run=_iso(nxt))
        jobs.append(job)
        _save(jobs)
    log("SCHEDULE", f"{r.name}: {c.expr} (next {job.next_run})")
    return {"status": "ok", "recipe": r.name, "cron": c.expr, "next_run": job.next_run,
            "note": "runs in the foreground with the Esc banner unless the recipe is background-capable; "
                    "deferred 5 minutes when the desktop is busy"}


def remove_job(name: str) -> dict[str, Any]:
    key = (name or "").strip().lower()
    with _lock:
        jobs = _load()
        keep = [j for j in jobs if j.recipe.lower() != key]
        if len(keep) == len(jobs):
            return {"status": "failed", "failure_reason": f"no schedule for {name!r}"}
        _save(keep)
    log("SCHEDULE", f"{name}: schedule removed")
    return {"status": "ok", "recipe": name}


# ---------------------------------------------------------------- the runner
class Scheduler:
    """Runs due jobs. `clock` and `replay` are injectable for the fake-clock tests."""

    def __init__(self, engine, clock: Callable[[], datetime] = datetime.now,
                 replay: Optional[Callable[..., dict[str, Any]]] = None) -> None:
        self.engine = engine
        self.clock = clock
        self.replay = replay or (lambda name, inputs: engine.replay(name, inputs, trigger="schedule"))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def due(self, now: Optional[datetime] = None) -> list[Job]:
        now = now or self.clock()
        out = []
        for j in list_jobs():
            try:
                if datetime.fromisoformat(j.next_run) <= now:
                    out.append(j)
            except ValueError:
                out.append(j)                                   # unreadable next_run: recompute by running
        return out

    def tick(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """Run every due job once; returns their outcomes. Called by the thread and by tests."""
        now = now or self.clock()
        results = []
        for j in self.due(now):
            r, why = _check(j.recipe, j.inputs)
            if r is None:
                out = {"status": "refused", "failure_reason": why}
            else:
                out = self.replay(j.recipe, dict(j.inputs))
            try:
                c = parse_cron(j.cron)
                if out.get("status") == "busy":
                    nxt = now + timedelta(seconds=DEFER_BUSY_S)
                    log("SCHEDULE", f"{j.recipe}: desktop busy, deferred to {_iso(nxt)}")
                else:
                    nxt = c.next_after(now)
            except CronError:
                nxt = now + timedelta(days=3650)
            with _lock:
                jobs = _load()
                for k in jobs:
                    if k.recipe == j.recipe:
                        k.next_run = _iso(nxt)
                        k.runs += 1
                        k.last = {"at": _iso(now), "status": out.get("status"), "seconds": out.get("seconds", 0),
                                  "failure_reason": out.get("failure_reason", "")}
                _save(jobs)
            results.append({"recipe": j.recipe, **out})
        return results

    def start(self) -> "Scheduler":
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        log("SCHEDULE", f"scheduler up ({len(list_jobs())} job(s))")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                log("SCHEDULE", f"tick failed: {e!r}")
            self._stop.wait(TICK_S)
