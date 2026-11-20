"""The one owner of the desktop, and the one caller of recipes.replay.

There is ONE OS cursor and ONE keyboard. Two things driving them at once would fight,
so every path that moves the mouse or types - a replay, a live primitive action, the
recorder - goes through `acquire_input()` on the single module-level `desktop_lock`.
Never replace it with a per-caller lock. A caller that cannot get it does not wait in
the dark: it either raises `InputBusy` (so an MCP tool can answer "busy" immediately)
or waits in a cancellable 250 ms poll with a visible "waiting" state.

`Engine.replay()` is the whole contract of replay_recipe: it decides refused /
quarantined / busy BEFORE touching the desktop, runs the verified replay, records the
outcome exactly once, writes the transcript line, and returns a structured result a
client model can act on without ever being told to "check the screen".

Lifted from the original tasks.py (desktop_lock + TaskManager._acquire) without
behavioural change.
"""
from __future__ import annotations

import base64
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Optional

from recipe_engine import abort, config, overlay, recipes, safety, sealed, transcripts, vdesk
from recipe_engine.config import log
from recipe_engine.recorder import Recorder

# The single global desktop lock. Never replace it with a per-task lock.
desktop_lock = threading.Lock()


class InputBusy(Exception):
    """Someone else owns the desktop right now (a replay, a live action, the recorder)."""


class InputCancelled(Exception):
    """The caller's cancel event was set while it was waiting for the desktop."""


# Who holds the lock, for engine_status and for "busy: recipe X is replaying".
_owner: dict = {"what": "", "since": 0.0}
_owner_lock = threading.Lock()


def input_owned() -> bool:
    return desktop_lock.locked()


def input_owner() -> dict:
    with _owner_lock:
        return dict(_owner)


@contextmanager
def acquire_input(what: str, cancel: Optional[threading.Event] = None, block: bool = False):
    """Own the desktop for the duration of the block.

    block=False (the MCP default): raise InputBusy at once if someone else has it.
    block=True: wait in a 250 ms poll that honours `cancel` (never an uninterruptible
    acquire), logging the wait so the panel can show it.
    """
    got = desktop_lock.acquire(blocking=False)
    if not got:
        if not block:
            raise InputBusy(f"the desktop is owned by {_owner.get('what') or 'another action'}")
        log("INPUT", f"{what}: waiting for the desktop (held by {_owner.get('what') or '?'})")
        while not got:
            if cancel is not None and cancel.is_set():
                raise InputCancelled()
            got = desktop_lock.acquire(timeout=0.25)
    with _owner_lock:
        _owner.update(what=what, since=time.time())
    try:
        yield
    finally:
        with _owner_lock:
            _owner.update(what="", since=0.0)
        desktop_lock.release()


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
STATUSES = ("ok", "failed", "quarantined", "refused", "busy")


def _outcome(status: str, *, seconds: float = 0.0, failed_step: Optional[int] = None, failure_reason: str = "",
             screenshot_b64: str = "", tried: Optional[list[str]] = None, background_mode: bool = False,
             read_texts: Optional[list[str]] = None, steps_done: int = 0) -> dict[str, Any]:
    assert status in STATUSES, status
    return {"status": status, "seconds": round(seconds, 2), "failed_step": failed_step,
            "failure_reason": failure_reason, "screenshot_b64": screenshot_b64,
            "approaches_already_tried": list(tried or []), "background_mode": background_mode,
            "read_texts": list(read_texts or []), "steps_done": steps_done}


def _screenshot_b64(surface: str) -> str:
    try:
        if surface == "desktop":
            from recipe_engine.screen import screenshot_jpeg
            jpeg, _ = screenshot_jpeg()
            return base64.b64encode(jpeg).decode("ascii")
    except Exception as e:
        log("ENGINE", f"failure screenshot skipped: {e!r}")
    return ""


class Engine:
    def __init__(self) -> None:
        self.recorder = Recorder()
        self.invariant_problems: list[str] = []
        self.started_at = time.time()
        self.control_ok = True
        self.control_message = ""
        self.background_available = False                # service.prepare sets it (pattern replay needs UIA)
        self.background_reason = "not probed"
        self.virtual_desktop = {"available": False, "reason": "not probed", "used": False}
        self.current: dict[str, Any] = {}
        self._state_lock = threading.Lock()
        self._current_cancel: Optional[threading.Event] = None

    # ------------------------------------------------------------- queries
    @property
    def input_owned(self) -> bool:
        from recipe_engine import desktop
        return desktop_lock.locked() or desktop._action_busy.is_set()

    def status(self) -> dict[str, Any]:
        from recipe_engine import __version__
        with self._state_lock:
            cur = dict(self.current)
        return {
            "version": __version__,
            "invariants_ok": not self.invariant_problems,
            "invariant_problems": list(self.invariant_problems),
            "control_ok": self.control_ok,
            "sealed_storage": sealed.sealed(),
            "control_message": self.control_message,
            "input_owned": self.input_owned,
            "input_owner": input_owner(),
            "recording": self.recorder.status(),
            "replaying": cur,
            "background_available": self.background_available,
            "background_reason": self.background_reason,
            "virtual_desktop": dict(self.virtual_desktop),
            "recipes": len(recipes.load_all()),
            "jobs": self._job_count(),
            "data_dir": str(config.DATA_DIR),
            "bind": f"{config.BIND_HOST}:{config.PORT}",
            "uptime_s": round(time.time() - self.started_at),
        }

    @staticmethod
    def _job_count() -> int:
        try:
            from recipe_engine import scheduler
            return len(scheduler.list_jobs())
        except Exception:
            return 0

    def _background_mode(self, r: recipes.Recipe) -> bool:
        """Decided ONCE, before the first step: pattern-only recipe AND pyvda works AND the
        setting is on. Never flips mid-run (that would steal the user's screen unannounced)."""
        if not getattr(r, "background_capable", False) or r.surface != "desktop":
            return False
        if not config.BACKGROUND_REPLAY:
            return False
        return bool(self.background_available)

    def _run_background(self, r: recipes.Recipe, cancel: threading.Event, steps: list[dict[str, Any]]) -> recipes.ReplayResult:
        """Pattern-driven replay IN PLACE: every step goes through a UIA provider pattern
        (Invoke / SetValue / Toggle / Select) - no SendInput, no focus or foreground change
        (the user's window is put back in front after every step if a provider pulled focus).
        The app stays on the user's desktop. Parking it on a scratch virtual desktop was
        tried and rejected: a window moved off the active desktop loses its UI Automation
        subtree (Notepad shows 6 nodes and no editor), so nothing could be resolved by
        identity there - see vdesk.py."""
        return recipes.replay(r, cancel, steps=steps, background=True)

    def abort(self) -> bool:
        """Stop the running replay at its next step (the panel's Stop, the Esc watch)."""
        with self._state_lock:
            ev = self._current_cancel
        if ev is None:
            return False
        ev.set()
        log("ENGINE", "abort requested: the replay stops at its next step")
        return True

    # ------------------------------------------------------------- replay
    def replay(self, name: str, inputs: Optional[dict[str, Any]] = None, *, cancel: Optional[threading.Event] = None,
               progress: Optional[Callable[[int, int], None]] = None, trigger: str = "mcp") -> dict[str, Any]:
        """The replay_recipe contract. Never raises for a task-level outcome."""
        t0 = time.time()
        inputs = dict(inputs or {})
        cancel = cancel or threading.Event()
        r = recipes.load_by_name(name)
        if r is None:
            return _outcome("failed", failure_reason=f"no_such_recipe: {name!r}")
        tried = transcripts.failed_reasons(r.id)

        def finish(out: dict[str, Any]) -> dict[str, Any]:
            out["approaches_already_tried"] = tried
            transcripts.append(r.id, {"name": r.name or r.label, "status": out["status"], "seconds": out["seconds"],
                                      "failed_step": out["failed_step"], "failure_reason": out["failure_reason"],
                                      "inputs_keys": sorted(inputs), "background_mode": out["background_mode"],
                                      "steps_done": out["steps_done"], "trigger": trigger})
            return out

        if r.quarantined:
            return finish(_outcome("quarantined", failure_reason="quarantined after repeated failures; re-record it"))
        text = " ".join([r.name, r.description] + [str(s.get("value") or "") + " " + str(s.get("selector") or "") + " "
                                                  + str((s.get("uia") or {}).get("name") or "") for s in r.steps])
        if safety.is_destructive(text):
            return finish(_outcome("refused", failure_reason=f"refused: {safety.destructive_reason(text)} - the engine never sends, submits, buys or deletes"))
        try:
            steps = recipes.substitute(r, inputs)
        except recipes.MissingParam as e:
            return finish(_outcome("refused", failure_reason=f"refused: {e}. Pass them in `inputs`."))
        except safety.CredentialRefused as e:
            return finish(_outcome("refused", failure_reason=f"refused: an input looks like a credential ({e}); the engine never types secrets"))
        try:
            with acquire_input(f"replay {r.name}", cancel=cancel, block=False):
                background = self._background_mode(r)
                with self._state_lock:
                    self.current = {"name": r.name or r.label, "id": r.id, "since": t0, "steps": len(r.steps),
                                    "background": background}
                    self._current_cancel = cancel
                watch = abort.AbortWatch(on_abort=cancel.set)
                try:
                    watch.start()
                    if background:
                        res = self._run_background(r, cancel, steps)
                    else:
                        overlay.banner().show()
                        try:
                            res = recipes.replay(r, cancel, steps=steps)
                        finally:
                            overlay.banner().hide()
                except recipes.ReplayLoginBlocked as e:
                    return finish(_outcome("failed", seconds=time.time() - t0, failure_reason=f"login_required: {e}",
                                           screenshot_b64=_screenshot_b64(r.surface)))
                except recipes.ReplayCancelled:
                    return finish(_outcome("failed", seconds=time.time() - t0, failure_reason="aborted_by_user",
                                           background_mode=background))
                except recipes.ReplayFailed as e:
                    recipes.record_outcome(r, False, time.time() - t0)
                    status = "quarantined" if r.quarantined else "failed"
                    return finish(_outcome(status, seconds=time.time() - t0, failed_step=e.step or None,
                                           failure_reason=e.reason, screenshot_b64=_screenshot_b64(r.surface),
                                           steps_done=e.steps_done))
                except Exception as e:
                    from recipe_engine.failures import short_reason
                    recipes.record_outcome(r, False, time.time() - t0)
                    return finish(_outcome("quarantined" if r.quarantined else "failed", seconds=time.time() - t0,
                                           failure_reason=f"error: {short_reason(repr(e))}",
                                           screenshot_b64=_screenshot_b64(r.surface)))
                finally:
                    watch.stop()
                    with self._state_lock:
                        self.current = {}
                        self._current_cancel = None
                recipes.record_outcome(r, True, res.duration_s)
                return finish(_outcome("ok", seconds=res.duration_s, read_texts=res.read_texts, steps_done=res.steps_done,
                                       background_mode=background))
        except InputBusy as e:
            return finish(_outcome("busy", failure_reason=str(e)))
        except InputCancelled:
            return finish(_outcome("failed", failure_reason="aborted_by_user"))


def _selftest() -> None:
    import queue

    order: "queue.Queue[str]" = queue.Queue()

    def worker(name: str, hold: float) -> None:
        with acquire_input(name, block=True):
            order.put(f"{name}:start")
            time.sleep(hold)
            order.put(f"{name}:end")

    a = threading.Thread(target=worker, args=("a", 0.3))
    b = threading.Thread(target=worker, args=("b", 0.05))
    a.start(); time.sleep(0.05); b.start()
    a.join(); b.join()
    seq = [order.get() for _ in range(4)]
    assert seq == ["a:start", "a:end", "b:start", "b:end"], seq
    with acquire_input("holder"):
        assert input_owned() and input_owner()["what"] == "holder"
        try:
            with acquire_input("intruder"):
                raise AssertionError("second owner got the desktop")
        except InputBusy:
            pass
    assert not input_owned()
    cancel = threading.Event()
    with acquire_input("holder2"):
        cancel.set()
        try:
            with acquire_input("waiter", cancel=cancel, block=True):
                raise AssertionError("cancelled waiter got the desktop")
        except InputCancelled:
            pass
    print("engine input-lock self-test ok")


if __name__ == "__main__":
    _selftest()
