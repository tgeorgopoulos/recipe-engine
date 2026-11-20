"""
Reliability study: record a desktop recipe ONCE, then replay it N times through
Engine.replay() - the same path replay_recipe takes - and report the numbers the
README claims: success rate (>= 95 %) and median replay time (< 10 s).

    python tests/reliability_study.py --runs 50
    python tests/reliability_study.py --runs 50 --apps notepad,calculator --report reports/reliability.md

Drives the REAL desktop (an unlocked, interactive session; keep your hands off the
keyboard while it runs; Esc aborts the current replay). Runs against a throwaway
data dir, so your real recipes are untouched.

Apps
  notepad     record: click the editor (UIA target, ValuePattern) + type a line;
              replay N times into a FRESH Notepad window each time (foreground mode,
              banner + Esc watch). Verified by the window title Notepad gives the text.
  calculator  record: 7 + 8 = by AutomationId (num7Button, plusButton, ...);
              every step is InvokePattern-driven, so the replays run in BACKGROUND
              mode: UI Automation patterns in place, no SendInput, focus restored
              after every step. Skipped with a plain reason when the Store
              Calculator is not installed (Server images).

Output: <report dir>/runs.jsonl (one record per replay) and report.md. Exit 1 when
any app misses the bar - this is the CI gate, not a demo.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RECIPE_ENGINE_DATA_DIR", tempfile.mkdtemp(prefix="recipe-engine-study-"))
os.environ.setdefault("RECIPE_ENGINE_UNSEALED", "1")

from recipe_engine import config, desktop, display, recipes, screen  # noqa: E402
from recipe_engine.engine import Engine  # noqa: E402
from recipe_engine.recorder import Recorder  # noqa: E402
from tests._notepad import _force_foreground, close_notepad, open_notepad  # noqa: E402

NOTE = "Recipe Engine reliability run"


@dataclass
class RunRecord:
    app: str
    run: int
    status: str
    seconds: float
    failed_step: Optional[int]
    failure_reason: str
    background_mode: bool
    verified: bool                      # our own check of the app's state, beyond the engine's


def _shot_xy(px: int, py: int) -> tuple[int, int]:
    """physical screen point -> screenshot coordinates (what the click primitive takes)."""
    screen.screenshot_jpeg()
    sw, sh = screen.shot_size()
    pw, ph = display.physical_screen_size()
    return int(px * sw / pw), int(py * sh / ph)


# ---------------------------------------------------------------- notepad
def record_notepad(name: str) -> recipes.Recipe:
    proc, win, hwnd = open_notepad()
    try:
        rec = Recorder()
        ok, why = rec.start(name, "type a line into a fresh Notepad window", ["Notepad"])
        assert ok, why
        r = win.BoundingRectangle
        x, y = _shot_xy((r.left + r.right) // 2, (r.top + r.bottom) // 2)
        desktop.click(x, y)
        desktop.type_text(NOTE)
        recipe, why = rec.stop()
        assert recipe is not None, why
        # the premise of the whole study: the trace must have landed in Notepad. If another
        # window was on top at record time, say so instead of replaying nonsense 50 times.
        expects = [str(st.get("expect") or "") for st in recipe.steps]
        assert all("Notepad" in e for e in expects), f"recording did not land in Notepad (expects: {expects})"
        return recipe
    finally:
        close_notepad(hwnd, proc)


def replay_notepad(eng: Engine, name: str, i: int) -> RunRecord:
    proc, win, hwnd = open_notepad()
    try:
        out = eng.replay(name, trigger="study")
        verified = recipes._wait_window(f"*{NOTE} - Notepad", 3000) is not None if out["status"] == "ok" else False
        return RunRecord("notepad", i, out["status"], out["seconds"], out["failed_step"], out["failure_reason"], out["background_mode"], verified)
    finally:
        close_notepad(hwnd, proc)


# ---------------------------------------------------------------- calculator
def _open_calc():
    import uiautomation as auto
    before = {w.NativeWindowHandle for w in auto.GetRootControl().GetChildren()}
    proc = subprocess.Popen(["calc.exe"])
    deadline = time.time() + 10
    win = None
    while time.time() < deadline and win is None:
        for w in auto.GetRootControl().GetChildren():
            if w.NativeWindowHandle not in before and (w.Name or "") == "Calculator":
                win = w
        time.sleep(0.25)
    if win is None:
        return proc, None, None, {}
    hwnd = win.NativeWindowHandle
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    # same premise as Notepad: the recording clicks buttons by position, so the window
    # must really be in front (a terminal or client may hold the foreground lock)
    assert _force_foreground(hwnd), "Calculator could not take the foreground; refusing to click blind"
    # the buttons appear once the XAML tree is up
    ids = {}
    deadline = time.time() + 8
    while time.time() < deadline and len(ids) < 5:
        for aid in ("num7Button", "plusButton", "num8Button", "equalButton", "CalculatorResults"):
            c = auto.Control(searchFromControl=win, AutomationId=aid)
            if c.Exists(0.5, 0.1):
                ids[aid] = c
        time.sleep(0.2)
    return proc, win, hwnd, ids


def _close_calc(proc, hwnd):
    try:
        if hwnd:
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def record_calculator(name: str) -> Optional[recipes.Recipe]:
    proc, win, hwnd, ids = _open_calc()
    try:
        if win is None or len(ids) < 5:
            return None
        rec = Recorder()
        ok, why = rec.start(name, "add 7 and 8 in Calculator", ["Calculator"])
        assert ok, why
        for aid in ("num7Button", "plusButton", "num8Button", "equalButton"):
            r = ids[aid].BoundingRectangle
            x, y = _shot_xy((r.left + r.right) // 2, (r.top + r.bottom) // 2)
            desktop.click(x, y)
            time.sleep(0.15)
        recipe, why = rec.stop()
        assert recipe is not None, why
        expects = [str(st.get("expect") or "") for st in recipe.steps]
        assert all("Calculator" in e for e in expects), f"recording did not land in Calculator (expects: {expects})"
        return recipe
    finally:
        _close_calc(proc, hwnd)


def replay_calculator(eng: Engine, name: str, i: int) -> RunRecord:
    import uiautomation as auto
    proc, win, hwnd, ids = _open_calc()
    try:
        out = eng.replay(name, trigger="study")
        verified = False
        if out["status"] == "ok" and win is not None:
            res = auto.Control(searchFromControl=auto.ControlFromHandle(hwnd), AutomationId="CalculatorResults")
            verified = res.Exists(2, 0.2) and "15" in (res.Name or "")
        return RunRecord("calculator", i, out["status"], out["seconds"], out["failed_step"], out["failure_reason"], out["background_mode"], verified)
    finally:
        _close_calc(proc, hwnd)


# ---------------------------------------------------------------- report
def summarize(app: str, runs: list[RunRecord]) -> dict:
    ok = [r for r in runs if r.status == "ok" and r.verified]
    times = [r.seconds for r in ok]
    reasons: dict[str, int] = {}
    for r in runs:
        if not (r.status == "ok" and r.verified):
            key = r.failure_reason or ("engine said ok but the app did not show the result" if r.status == "ok" else r.status)
            reasons[key] = reasons.get(key, 0) + 1
    return {"app": app, "runs": len(runs), "ok": len(ok), "rate": (len(ok) / len(runs)) if runs else 0.0,
            "median_s": statistics.median(times) if times else None, "mean_s": statistics.mean(times) if times else None,
            "max_s": max(times) if times else None, "background": sum(1 for r in runs if r.background_mode),
            "reasons": sorted(reasons.items(), key=lambda kv: -kv[1])}


def write_report(path: Path, summaries: list[dict], runs: list[RunRecord], skipped: dict[str, str], bar_rate: float, bar_median: float) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name("runs.jsonl"), "w", encoding="utf-8") as f:
        for r in runs:
            f.write(json.dumps(asdict(r)) + "\n")
    lines = ["# Recipe Engine reliability study", "",
             f"Run: {time.strftime('%Y-%m-%d %H:%M')} local time, {len(runs)} replays, through Engine.replay() (the replay_recipe path).",
             f"Bar: success rate >= {bar_rate:.0%}, median replay < {bar_median:.0f} s. Display: {display.current_display()}.", ""]
    passed = True
    for s in summaries:
        med = f"{s['median_s']:.2f} s" if s["median_s"] is not None else "n/a"
        good = s["rate"] >= bar_rate and s["median_s"] is not None and s["median_s"] < bar_median
        passed &= good
        lines += [f"## {s['app']}: {s['ok']}/{s['runs']} ok ({s['rate']:.0%}), median {med}, mean "
                  f"{(s['mean_s'] or 0):.2f} s, max {(s['max_s'] or 0):.2f} s, background replays {s['background']} - "
                  f"{'PASS' if good else 'FAIL'}"]
        for reason, n in s["reasons"]:
            lines.append(f"- {n}x {reason}")
        lines.append("")
    for app, why in skipped.items():
        lines += [f"## {app}: skipped - {why}", ""]
    lines.append(f"Result: {'PASS' if passed else 'FAIL'}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return passed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--apps", default="notepad,calculator")
    ap.add_argument("--report", default=str(ROOT / "reports" / "reliability" / "report.md"))
    ap.add_argument("--min-rate", type=float, default=0.95)
    ap.add_argument("--max-median", type=float, default=10.0)
    a = ap.parse_args(argv)
    display.make_dpi_aware()
    eng = Engine()
    # the same decision service.prepare() makes for the real host: background replay is
    # available when UI Automation answers (patterns in place), not when pyvda works
    from recipe_engine import uia
    st = uia.self_test()
    eng.background_available = bool(st.get("uia_ok"))
    eng.background_reason = "UI Automation ok (pattern-driven replay in place)" if eng.background_available else f"UI Automation unavailable: {st.get('error')}"
    print(f"data dir {config.DATA_DIR}; background replay: {eng.background_reason}")
    runs: list[RunRecord] = []
    summaries = []
    skipped: dict[str, str] = {}
    for app in [x.strip() for x in a.apps.split(",") if x.strip()]:
        name = f"study-{app}"
        recorder, replayer = {"notepad": (record_notepad, replay_notepad), "calculator": (record_calculator, replay_calculator)}[app]
        print(f"[{app}] recording")
        recipe = recorder(name)
        if recipe is None:
            skipped[app] = "app not available on this machine"
            print(f"[{app}] skipped: not available")
            continue
        print(f"[{app}] recorded {len(recipe.steps)} steps, background_capable={recipe.background_capable}")
        app_runs = []
        for i in range(1, a.runs + 1):
            rr = replayer(eng, name, i)
            app_runs.append(rr)
            print(f"[{app}] {i:3d}/{a.runs} {rr.status:11s} {rr.seconds:5.2f}s bg={rr.background_mode} verified={rr.verified} {rr.failure_reason[:60]}")
            if rr.status == "quarantined":
                r = recipes.load_by_name(name)
                if r is not None:
                    r.quarantined, r.consecutive_failures = False, 0     # keep measuring; the count is in the report
                    recipes.save(r)
        runs += app_runs
        summaries.append(summarize(app, app_runs))
        r = recipes.load_by_name(name)
        if r is not None:
            recipes.forget(r.id)
    passed = write_report(Path(a.report), summaries, runs, skipped, a.min_rate, a.max_median)
    print(Path(a.report).read_text(encoding="utf-8"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
