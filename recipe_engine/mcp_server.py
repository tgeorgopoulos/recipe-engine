"""The MCP tool surface. Three groups:

* recipe tools - list/record/stop/replay/stats/forget/schedule/export/import/status.
  `replay_recipe` is the product: blocking, verified, zero model work, and its
  description tells a client model to PREFER it whenever the user says "again",
  "same as last time", "like before", "every Monday" or names a recipe.
* desktop primitives - screenshot/click/double_click/right_click/move/type/key/
  scroll/wait/open_browser. Claude acts on the host through these; recording is a
  trace of these calls.
* browser primitives - browser_goto/... /browser_download over the engine's own
  signed-in Chrome profile.

Every mutating tool refuses while the invariant self-test has failed, answers
"busy" instead of queueing when the desktop is owned, and turns a CredentialRefused
into a refusal that also pauses any recording in progress.
"""
from __future__ import annotations

import asyncio
import base64
import threading
import time
from typing import Any, Literal, Optional, TypedDict

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.types import ToolAnnotations


class ReplayOutcome(TypedDict):
    """What replay_recipe returns. Structured on the wire so a client model reads
    fields, never prose."""
    status: Literal["ok", "failed", "quarantined", "refused", "busy"]
    seconds: float
    failed_step: Optional[int]
    failure_reason: str
    screenshot_b64: str
    approaches_already_tried: list[str]
    background_mode: bool
    read_texts: list[str]
    steps_done: int

from recipe_engine import __version__, config, desktop, engine as engine_mod, recipes, safety, transcripts
from recipe_engine.config import log
from recipe_engine.engine import Engine, InputBusy

INSTRUCTIONS = """Recipe Engine replays desktop and browser workflows that were already performed once
through its own tools, deterministically and without any model work.

Decision rule: if the user says "again", "same as last time", "like before", "the usual",
"every <day>", or names something from list_recipes, call replay_recipe FIRST. Only drive the
screen with the primitive tools when there is no recipe, or when replay_recipe reports a
failure whose approaches_already_tried you can avoid. To make a workflow replayable, call
record_recipe before performing it with the primitive tools, then stop_recording.

The engine will never type a password or code, never send/submit/buy/post/delete, and never
run two things on the desktop at once - those come back as "refused" or "busy", not as
something for you to work around."""

READ = ToolAnnotations(read_only_hint=True)
ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)

_REFUSED_INVARIANTS = "invariants_failed: the engine refuses to act until its safety self-test passes (see engine_status)"


def _shot(res) -> list:
    """desktop ActionResult -> [Image, note]."""
    out: list[Any] = []
    if res.jpeg:
        out.append(Image(data=res.jpeg, format="jpeg"))
    out.append(res.note if res.ok else f"error: {res.error}")
    return out


def _from_op(result: dict[str, Any]) -> list:
    """browser op dict {'content':[{type:text|image}], 'is_error'} -> [Image?, str]."""
    out: list[Any] = []
    for block in result.get("content") or []:
        if block.get("type") == "image":
            out.append(Image(data=base64.b64decode(block.get("data", "")), format="jpeg"))
        elif block.get("type") == "text":
            out.append(("error: " if result.get("is_error") else "") + str(block.get("text", "")))
    return out or ["(no output)"]


def build_server(eng: Engine) -> MCPServer:
    server = MCPServer(name="recipe-engine", version=__version__, instructions=INSTRUCTIONS)

    def guard() -> Optional[str]:
        return _REFUSED_INVARIANTS if eng.invariant_problems else None

    # ------------------------------------------------------------- recipe tools
    @server.tool(name="list_recipes", description="Every saved recipe with its success rate, last run, typical "
                 "duration and status (ready | quarantined | expired). Check this before driving the screen: if "
                 "one matches what the user wants, replay_recipe it.", annotations=READ)
    def list_recipes() -> dict:
        rows = []
        for r in recipes.load_all():
            runs = r.runs or {}
            ok, bad = int(runs.get("success", 0)), int(runs.get("fail", 0))
            status = "quarantined" if r.quarantined else ("expired" if r.stale else "ready")
            rows.append({"name": r.name or r.command_normalized, "description": r.description, "surface": r.surface,
                         "apps": list(r.app_hints or []), "steps": len(r.steps),
                         "success_rate": round(ok / (ok + bad), 2) if ok + bad else None,
                         "runs": ok + bad, "last_run": r.last_used or None, "avg_seconds": round(float(r.avg_duration_s or 0), 1),
                         "status": status})
        rows.sort(key=lambda x: x["last_run"] or "", reverse=True)
        return {"recipes": rows, "count": len(rows)}

    @server.tool(name="record_recipe", description="Start recording. Every desktop/browser primitive you call from "
                 "now until stop_recording becomes a step of a new recipe named `name`. Names are kebab-case "
                 "(weekly-timesheet). Recording pauses itself on a login page or a password field and refuses "
                 "destructive workflows (send, submit, buy, delete, post).", annotations=ACT)
    def record_recipe(name: str, description: str = "", app_hints: list[str] | None = None) -> dict:
        if (g := guard()):
            return {"recording": False, "reason": g}
        ok, reason = eng.recorder.start(name, description, app_hints or [])
        return {"recording": ok, "reason": reason, "name": name}

    @server.tool(name="stop_recording", description="Stop recording and save the recipe. Returns saved=false with the "
                 "reason when the run cannot be a recipe (login page seen, credential refused, destructive step, "
                 "polling).", annotations=ACT)
    def stop_recording() -> dict:
        r, reason = eng.recorder.stop()
        if r is None:
            return {"saved": False, "reason": reason}
        return {"saved": True, "name": r.name, "steps": len(r.steps), "surface": r.surface,
                "preview": recipes.describe_steps(r)[:2000], "reason": reason}

    @server.tool(name="replay_recipe", description="Replay a saved recipe with ZERO model work: deterministic, "
                 "verified step by step, seconds not minutes. PREFER THIS over driving the screen whenever the "
                 "user says 'again', 'same as last time', 'like before', 'every Monday', or names a recipe from "
                 "list_recipes. BLOCKING: returns only when the replay has finished. Never asks you to check the "
                 "screen - read `status`: ok | failed (failed_step + failure_reason + screenshot) | quarantined "
                 "(re-record it) | refused (destructive or missing inputs; do not retry) | busy (the desktop is in "
                 "use; retry later). On `failed`, avoid every approach in approaches_already_tried.",
                 annotations=ACT, structured_output=True)
    async def replay_recipe(name: str, inputs: dict[str, str] | None = None, ctx: Context | None = None) -> ReplayOutcome:
        if (g := guard()):
            return engine_mod._outcome("refused", failure_reason=g)  # type: ignore[return-value]
        cancel = threading.Event()
        if ctx is not None:
            try:
                await ctx.report_progress(0, 1, f"replaying {name}")
            except Exception:
                pass
        task = asyncio.to_thread(eng.replay, name, dict(inputs or {}), cancel=cancel, trigger="mcp")
        try:
            out = await asyncio.wait_for(task, timeout=config.RECIPE_MAX_REPLAY_S)
        except asyncio.TimeoutError:
            cancel.set()
            return engine_mod._outcome("failed", seconds=float(config.RECIPE_MAX_REPLAY_S),
                                       failure_reason=f"timeout: replay exceeded {config.RECIPE_MAX_REPLAY_S}s and was stopped at its next step")
        if ctx is not None:
            try:
                await ctx.report_progress(1, 1, f"{name}: {out['status']}")
            except Exception:
                pass
        return out

    @server.tool(name="recipe_stats", description="What the harness knows about a recipe, in plain English: how "
                 "often it ran, how often it failed and at which step, typical duration, quarantine state.",
                 annotations=READ)
    def recipe_stats(name: str) -> str:
        r = recipes.load_by_name(name)
        if r is None:
            return f"No recipe named {name!r}."
        return transcripts.stats_text(r)

    @server.tool(name="forget_recipe", description="Delete a recipe and its transcript.", annotations=ACT)
    def forget_recipe(name: str) -> dict:
        r = recipes.load_by_name(name)
        if r is None:
            return {"forgotten": False, "reason": f"no recipe named {name!r}"}
        ok = recipes.forget(r.id)
        try:
            transcripts.path_for(r.id).unlink(missing_ok=True)
        except OSError:
            pass
        return {"forgotten": bool(ok), "name": r.name or r.command_normalized}

    @server.tool(name="schedule_recipe", description="Run a recipe on a cron schedule (5 fields, local time: "
                 "'0 9 * * 1' = Mondays 09:00; @daily/@weekly/@monthly accepted) with fixed inputs. Refused for "
                 "quarantined or destructive recipes and when a {{parameter}} is missing. A job that comes due while "
                 "the desktop is busy is deferred 5 minutes.", annotations=ACT)
    def schedule_recipe(name: str, cron: str, inputs: dict[str, str] | None = None) -> dict:
        if (g := guard()):
            return {"status": "refused", "failure_reason": g}
        from recipe_engine import scheduler
        return scheduler.add_job(name, cron, dict(inputs or {}))

    @server.tool(name="unschedule_recipe", description="Remove a recipe's schedule.", annotations=ACT)
    def unschedule_recipe(name: str) -> dict:
        if (g := guard()):
            return {"status": "refused", "failure_reason": g}
        from recipe_engine import scheduler
        return scheduler.remove_job(name)

    @server.tool(name="export_recipe", description="Export a recipe as signed JSON for another machine (returns the "
                 "text in `json`). Carries the steps, parameters and app hints only - never run history, cookies, "
                 "browser storage or any credential-looking text (such a recipe is refused).", annotations=READ)
    def export_recipe(name: str) -> dict:
        from recipe_engine import export as export_mod
        return export_mod.export_recipe(name)

    @server.tool(name="import_recipe", description="Import a recipe exported by export_recipe. The safety checks run "
                 "again here; the recipe starts with zero runs and no quarantine. signature_ok=false means it was "
                 "signed by another install or edited since. `replace` overwrites a recipe with the same name.",
                 annotations=ACT)
    def import_recipe(json: str, replace: bool = False) -> dict:
        if (g := guard()):
            return {"status": "refused", "failure_reason": g}
        from recipe_engine import export as export_mod
        return export_mod.import_recipe(json, replace=replace)

    @server.tool(name="engine_status", description="Version, invariant self-test result, whether the desktop is "
                 "currently owned (input_owned), what is recording/replaying, and background-mode availability.",
                 annotations=READ)
    def engine_status() -> dict:
        return eng.status()

    # ------------------------------------------------------------- desktop primitives
    def _act(what: str, fn, *args):
        """Run a desktop action as the desktop's owner. 'busy' if someone else has it."""
        if (g := guard()):
            return [g]
        try:
            with engine_mod.acquire_input(what, block=False):
                return _shot(fn(*args))
        except InputBusy as e:
            return [f"busy: {e}"]
        except safety.CredentialRefused as e:
            eng.recorder.pause("credential refused")
            log("TOOL", f"{what}: refused - {e}")
            return [f"refused: {e} Nothing was typed. Ask the user to enter it themselves."]

    @server.tool(name="screenshot", description="JPEG of the primary display. Coordinates you pass to click/type "
                 "are in THIS image's pixel space. Call it first and after anything that changes the screen.",
                 annotations=READ)
    def screenshot() -> list:
        if (g := guard()):
            return [g]
        return _shot(desktop.screenshot())

    @server.tool(name="click", description="Left-click at (x, y) in screenshot coordinates.", annotations=ACT)
    def click(x: int, y: int) -> list:
        return _act("click", desktop.click, x, y)

    @server.tool(name="double_click", description="Double-click at (x, y) in screenshot coordinates.", annotations=ACT)
    def double_click(x: int, y: int) -> list:
        return _act("double_click", desktop.double_click, x, y)

    @server.tool(name="right_click", description="Right-click at (x, y) in screenshot coordinates.", annotations=ACT)
    def right_click(x: int, y: int) -> list:
        return _act("right_click", desktop.right_click, x, y)

    @server.tool(name="move", description="Move the mouse to (x, y) without clicking (hover menus).", annotations=ACT)
    def move(x: int, y: int) -> list:
        return _act("move", desktop.move, x, y)

    @server.tool(name="type", description="Type text into the focused field. Newlines are typed as shift+enter, so "
                 "send a WHOLE multi-line message in ONE call. NEVER use this for passwords, passcodes, PINs or "
                 "verification codes - it refuses and pauses any recording. While recording, pass `param` to "
                 "make this value a recipe parameter (e.g. param='week'): the recipe stores {{week}} and "
                 "replay_recipe fills it from inputs.", annotations=ACT)
    def type_text(text: str, param: str = "") -> list:
        if param and eng.recorder.active:
            eng.recorder.mark_param(param)
        return _act("type", desktop.type_text, text)

    @server.tool(name="key", description="Press a key or combination: 'enter', 'ctrl+s', 'alt+tab', 'win', 'esc'.", annotations=ACT)
    def key(combo: str) -> list:
        return _act("key", desktop.key, combo)

    @server.tool(name="scroll", description="Scroll at (x, y). Positive clicks scroll up, negative down.", annotations=ACT)
    def scroll(x: int, y: int, clicks: int) -> list:
        return _act("scroll", desktop.scroll, x, y, clicks)

    @server.tool(name="wait", description="Wait up to 10 seconds for something to load, then screenshot.", annotations=READ)
    def wait(seconds: float = 1.0) -> list:
        if (g := guard()):
            return [g]
        return _shot(desktop.wait(seconds))

    @server.tool(name="open_browser", description="Open a URL as a tab of the engine's own signed-in Chrome "
                 "profile. Use the browser_* tools to act on the page.", annotations=ACT)
    def open_browser(url: str) -> list:
        return _act("open_browser", desktop.open_browser, url)

    # ------------------------------------------------------------- browser primitives
    from recipe_engine import browser

    state: dict[str, Any] = {"sess": None}

    def session() -> "browser.BrowserSession":
        if state["sess"] is None:
            state["sess"] = browser.BrowserSession(browser.service())
        return state["sess"]

    async def bop(name: str, args: dict[str, Any]) -> list:
        if (g := guard()):
            return [g]
        sess = session()
        before = len(sess.trace)
        desktop._action_busy.set()
        try:
            result = await sess.svc.call(browser.OPS[name](sess, args))
        except Exception as e:
            return [f"error: {name} failed: {type(e).__name__}: {str(e)[:300]}"]
        finally:
            desktop._action_busy.clear()
        if len(sess.trace) > before and eng.recorder.active:
            for step in sess.trace[before:]:
                eng.recorder.record(step)
        if sess.login_seen and eng.recorder.active:
            eng.recorder.pause("login page seen")
        return _from_op(result)

    sel = "Selector from browser_snapshot: role=button[name='Save'], text=Invoices, label=Search, placeholder=Email, testid=submit, css=#id"

    @server.tool(name="browser_goto", description="Navigate the engine's tab to a URL. Returns the accessibility snapshot. "
                 "While recording, `param` makes the URL a recipe parameter.", annotations=ACT)
    async def browser_goto(url: str, param: str = "") -> list:
        if param and eng.recorder.active:
            eng.recorder.mark_param(param)
        return await bop("goto", {"url": url})

    @server.tool(name="browser_snapshot", description="Accessibility snapshot of the current page (roles + names). Act only through selectors taken from it.", annotations=READ)
    async def browser_snapshot() -> list:
        return await bop("snapshot", {})

    @server.tool(name="browser_click", description=f"Click an element. {sel}", annotations=ACT)
    async def browser_click(selector: str) -> list:
        return await bop("click", {"selector": selector})

    @server.tool(name="browser_fill", description=f"Clear and type into an input/textarea. NEVER for passwords or codes - it refuses. "
                 f"While recording, `param` makes the text a recipe parameter. {sel}", annotations=ACT)
    async def browser_fill(selector: str, text: str, param: str = "") -> list:
        if param and eng.recorder.active:
            eng.recorder.mark_param(param)
        out = await bop("fill", {"selector": selector, "text": text})
        if any(isinstance(o, str) and "forbidden" in o.lower() for o in out) and eng.recorder.active:
            eng.recorder.pause("credential refused")
        return out

    @server.tool(name="browser_press", description="Press a key ('Enter', 'Escape', 'Tab', 'Control+a'); optional selector focuses that element first.", annotations=ACT)
    async def browser_press(key: str, selector: str = "") -> list:
        return await bop("press", {"key": key, "selector": selector})

    @server.tool(name="browser_select", description="Choose an option in a <select> by visible label (or value).", annotations=ACT)
    async def browser_select(selector: str, value: str) -> list:
        return await bop("select", {"selector": selector, "value": value})

    @server.tool(name="browser_check", description="Tick a checkbox or radio.", annotations=ACT)
    async def browser_check(selector: str) -> list:
        return await bop("check", {"selector": selector})

    @server.tool(name="browser_wait_for", description="Wait until an element is visible (up to the action timeout). Use this instead of polling snapshot().", annotations=READ)
    async def browser_wait_for(selector: str) -> list:
        return await bop("wait_for", {"selector": selector})

    @server.tool(name="browser_read", description="Visible text of an element, or the whole page when selector is empty.", annotations=READ)
    async def browser_read(selector: str = "") -> list:
        return await bop("read", {"selector": selector})

    @server.tool(name="browser_screenshot", description="JPEG of the current viewport, for looking only.", annotations=READ)
    async def browser_screenshot() -> list:
        return await bop("screenshot", {})

    @server.tool(name="browser_scroll", description="Scroll the page 'down' or 'up' by `amount` pixels (default 600).", annotations=ACT)
    async def browser_scroll(direction: str, amount: int = 600) -> list:
        return await bop("scroll", {"direction": direction, "amount": amount})

    @server.tool(name="browser_back", description="Browser back.", annotations=ACT)
    async def browser_back() -> list:
        return await bop("back", {})

    @server.tool(name="browser_download", description="Click a link/button that downloads a file and SAVE it. "
                 "Returns the absolute saved path; use that exact path later to attach the file.", annotations=ACT)
    async def browser_download(selector: str) -> list:
        return await bop("download", {"selector": selector})

    return server


def tool_names(server: MCPServer) -> list[str]:
    try:
        return sorted(t.name for t in server._tool_manager.list_tools())  # type: ignore[attr-defined]
    except Exception:
        return []
