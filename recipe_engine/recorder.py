"""Records the NEXT sequence of engine actions Claude performs into a recipe.

`record_recipe(name, ...)` arms the recorder; every desktop action (desktop._rec) and
every browser action (the MCP layer forwards BrowserSession.record's step) lands in
`record()`; `stop_recording()` turns the trace into a recipe through
recipes.capture(), which applies the same guards as before: a run that saw a login
page, tripped the credential hook, was destructive, or polled is never saved.

Two things the recorder itself enforces, before capture ever sees the trace:
* destructive steps are DROPPED as they happen and remembered in `destructive_hit`,
  so the recipe cannot contain a Send/Buy/Delete even if the run continues;
* a login page or a refused credential PAUSES recording - later steps are ignored,
  stop_recording() reports why, and nothing is saved. The user signs in by hand and
  records again.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any, Optional

from recipe_engine import config, desktop, recipes, safety
from recipe_engine.config import log

_NAME_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


def _step_text(step: dict[str, Any]) -> str:
    """The words a step carries, for the destructive check: typed text, key combos,
    selectors (role=button[name='Send']) and URLs."""
    bits = [str(step.get("value") or ""), str(step.get("selector") or "")]
    uia = step.get("uia") or {}
    if isinstance(uia, dict):
        bits.append(str(uia.get("name") or ""))
    return " ".join(b for b in bits if b)


class Recorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = "idle"                  # idle | recording | paused
        self.name = ""
        self.description = ""
        self.app_hints: list[str] = []
        self.trace: list[dict[str, Any]] = []
        self.t0 = 0.0
        self.pause_reason = ""
        self.destructive_hit = ""
        self.login_seen = False
        self.credential_flagged = False
        self.params: list[dict[str, Any]] = []
        self._next_param = ""

    def mark_param(self, name: str) -> None:
        """The NEXT typed/filled/navigated value is a parameter: the recipe stores
        {{name}} and replay_recipe fills it from `inputs`."""
        name = (name or "").strip()
        if name and __import__("re").fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            self._next_param = name

    # ------------------------------------------------------------- queries
    @property
    def active(self) -> bool:
        return self.state in ("recording", "paused")

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self.state, "name": self.name, "steps": len(self.trace),
                    "paused_reason": self.pause_reason, "destructive_hit": self.destructive_hit,
                    "seconds": round(time.time() - self.t0, 1) if self.active else 0.0}

    # ------------------------------------------------------------- control
    def start(self, name: str, description: str, app_hints: Optional[list[str]] = None) -> tuple[bool, str]:
        name = (name or "").strip().lower()
        if not _NAME_RE.match(name):
            return False, "name must be 2-64 chars of a-z, 0-9 and '-', starting with a letter or digit"
        if safety.is_destructive(f"{name} {description or ''}"):
            return False, f"refused: the recipe would be destructive ({safety.destructive_reason(f'{name} {description or ''}')})"
        if recipes.load_by_name(name) is not None:
            return False, f"a recipe named {name!r} already exists; forget it first or pick another name"
        with self._lock:
            if self.active:
                return False, f"already recording {self.name!r}; call stop_recording first"
            self.state, self.name, self.description = "recording", name, (description or "").strip()
            self.app_hints = [str(h) for h in (app_hints or []) if str(h).strip()]
            self.trace, self.t0 = [], time.time()
            self.params, self._next_param = [], ""
            self.pause_reason = self.destructive_hit = ""
            self.login_seen = self.credential_flagged = False
        desktop._recorder_hook = self.record
        log("RECORD", f"recording {name!r}: every engine action from now on is a step")
        return True, "recording"

    def record(self, step: dict[str, Any]) -> None:
        """Called from the action layer. Never raises."""
        try:
            with self._lock:
                if self.state != "recording":
                    return
                text = _step_text(step)
                if text and safety.is_destructive(text):
                    self.destructive_hit = safety.destructive_reason(text) or "destructive step"
                    log("RECORD", f"dropped step {step.get('action')!r}: {self.destructive_hit}")
                    return
                step = dict(step)
                if self._next_param and step.get("action") in ("type", "fill", "goto"):
                    name = self._next_param
                    self.params.append({"name": name, "example": str(step.get("value") or "")[:40],
                                        "used_in": [len(self.trace) + 1]})
                    step["value"] = "{{" + name + "}}"
                    self._next_param = ""
                    log("RECORD", f"step {len(self.trace) + 1} takes parameter {{{{{name}}}}}")
                self.trace.append(step)
        except Exception as e:
            log("RECORD", f"record() failed: {e!r}")

    def pause(self, reason: str) -> None:
        with self._lock:
            if self.state == "recording":
                self.state, self.pause_reason = "paused", reason
                if "login" in reason:
                    self.login_seen = True
                if "credential" in reason or "password" in reason:
                    self.credential_flagged = True
                log("RECORD", f"paused: {reason}. This recipe will not be saved.")

    def stop(self) -> tuple[Optional[recipes.Recipe], str]:
        with self._lock:
            if not self.active:
                return None, "not recording"
            desktop._recorder_hook = None
            state, name, desc, hints = self.state, self.name, self.description, self.app_hints
            trace, dur = list(self.trace), time.time() - self.t0
            params = list(self.params)
            reason, dh = self.pause_reason, self.destructive_hit
            login_seen, cred = self.login_seen, self.credential_flagged
            self.state = "idle"
        if state == "paused":
            return None, f"not saved: recording was paused ({reason})"
        surfaces = [s.get("surface") for s in trace if s.get("surface")]
        surface = next((s for s in surfaces if s), "desktop")
        if any(s != surface for s in surfaces):
            return None, "not saved: a recipe must stay on one surface (all browser or all desktop)"
        result = SimpleNamespace(login_blocked=False, login_seen=login_seen, credential_flagged=cred,
                                 cancelled=False, error=None, hit_turn_limit=False, trace=trace, summary="")
        display = None
        if surface == "desktop":
            from recipe_engine.display import current_display
            display = current_display()
        r = recipes.capture(name, result, surface, dur, destructive=False, display=display,
                            description=desc or name, name=name, app_hints=hints, params=params)
        if r is None:
            why = recipes.should_capture(result, destructive=False)[1]
            tail = f"; a destructive step was dropped ({dh})" if dh else ""
            return None, f"not saved: {why}{tail}"
        tail = f" (a destructive step was dropped: {dh})" if dh else ""
        log("RECORD", f"saved {r.id} {name!r}: {len(r.steps)} steps in {dur:.1f}s{tail}")
        return r, f"saved {len(r.steps)} steps{tail}"
