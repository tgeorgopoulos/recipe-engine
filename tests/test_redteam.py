"""Safety red-team: every way a client could get the engine to type a secret or press
Send, and the engine's answer to each. Unit tests use a fake keyboard/page so nothing
real is touched; the last test drives a real WinForms password box (-m integration).

  1. type into a password field (UIA IsPassword)       -> refused, no key pressed, recording paused
  2. type into a field NAMED Password / CVV / Code    -> refused
  3. type credential-looking text into a plain field  -> refused (text gate)
  4. browser_fill: selector says password / text does / type=password -> refused, login_seen
  5. a replay step that would fill a credential       -> ReplayFailed before the fill
  6. Send / Buy / Submit / Delete: refused for replay, dropped while recording,
     a recipe whose UIA target is called "Send" is refused even with an innocent name
  7. the whole tool surface refuses while the invariant self-test has failed
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import pytest

from recipe_engine import desktop, recipes, safety, uia
from recipe_engine.engine import Engine
from recipe_engine.mcp_server import build_server
from recipe_engine.screen import ActionResult
from recipe_engine.uia import UiaTarget


class _FakePG:
    def __init__(self):
        self.calls: list[tuple] = []
        self.FAILSAFE = True
        self.PAUSE = 0.0

    def write(self, text, interval=0):
        self.calls.append(("write", text))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", "+".join(keys)))

    def press(self, k):
        self.calls.append(("press", k))


@pytest.fixture
def keyboard(monkeypatch):
    pg = _FakePG()
    monkeypatch.setattr(desktop, "_pyautogui", lambda: pg)
    monkeypatch.setattr(desktop, "settle_and_shoot", lambda note: ActionResult(note=note))
    monkeypatch.setattr(desktop, "foreground_window_title", lambda: "Sign in")
    monkeypatch.setattr(desktop, "shot_size", lambda: (1280, 720))
    return pg


def _focus(monkeypatch, target):
    monkeypatch.setattr(uia, "describe_focused", lambda: target)


def _call(server, tool, **args):
    res = asyncio.run(server.call_tool(tool, args))
    sc = getattr(res, "structured_content", None)
    if sc:
        return sc["result"] if set(sc) == {"result"} else sc
    texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
    try:
        return json.loads(texts[0]) if len(texts) == 1 else texts
    except (ValueError, IndexError):
        return texts


# ------------------------------------------------------------- 1. IsPassword
def test_typing_into_a_password_field_is_refused_whatever_the_text(keyboard, monkeypatch):
    _focus(monkeypatch, UiaTarget(control_type="EditControl", class_name="Edit", is_password=True,
                                  ancestors=[{"control_type": "WindowControl", "name": "Sign in", "class_name": "#32770"}]))
    with pytest.raises(safety.CredentialRefused) as ei:
        desktop.type_text("hello world")                 # innocent text: the FIELD is the problem
    assert "IsPassword" in str(ei.value)
    assert keyboard.calls == [], "keys were pressed into a password field"


def test_password_field_refusal_pauses_the_recording_so_it_is_never_saved(keyboard, monkeypatch):
    _focus(monkeypatch, UiaTarget(control_type="EditControl", is_password=True))
    srv = build_server(Engine())
    assert _call(srv, "record_recipe", name="portal-login", description="open the portal")["recording"] is True
    out = _call(srv, "type", text="hello")
    assert any("refused" in str(x).lower() and "forbidden" in str(x).lower() for x in out), out
    stopped = _call(srv, "stop_recording")
    assert stopped["saved"] is False and "paused" in stopped["reason"], stopped
    assert recipes.load_by_name("portal-login") is None


# ------------------------------------------------------------- 2. named fields
@pytest.mark.parametrize("target", [
    UiaTarget(control_type="EditControl", name="Password"),
    UiaTarget(control_type="EditControl", name="Confirm password"),
    UiaTarget(control_type="EditControl", automation_id="txtCVV"),
    UiaTarget(control_type="EditControl", name="Security code"),
    UiaTarget(control_type="EditControl", name="Enter the one-time code"),
    UiaTarget(control_type="EditControl", name="", ancestors=[{"control_type": "GroupControl", "name": "Card security code (CVC)"}]),
], ids=["password", "confirm", "cvv-id", "security-code", "otp", "parent-group"])
def test_fields_named_like_secrets_are_refused(keyboard, monkeypatch, target):
    _focus(monkeypatch, target)
    with pytest.raises(safety.CredentialRefused):
        desktop.type_text("4242")
    assert keyboard.calls == []


def test_an_ordinary_field_is_typed_into(keyboard, monkeypatch):
    _focus(monkeypatch, UiaTarget(control_type="DocumentControl", name="Text editor", class_name="RichEditD2DPT",
                                  ancestors=[{"control_type": "WindowControl", "name": "Untitled - Notepad", "class_name": "Notepad"}]))
    assert desktop.type_text("hours for week 12").ok
    assert keyboard.calls == [("write", "hours for week 12")]


def test_when_uia_cannot_see_the_focus_the_text_gate_still_applies(keyboard, monkeypatch):
    _focus(monkeypatch, None)
    with pytest.raises(safety.CredentialRefused):
        desktop.type_text("verification code 123456")
    assert desktop.type_text("plain note").ok
    assert keyboard.calls == [("write", "plain note")]


# ------------------------------------------------------------- 4. browser fill
class _Loc:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    async def get_attribute(self, name, timeout=None):
        return "password" if "pw" in self.sel else "text"

    async def fill(self, text, timeout=None):
        self.page.filled.append((self.sel, text))


class _Page:
    url = "http://127.0.0.1:8765/login"

    def __init__(self):
        self.filled = []

    def locator(self, sel):
        return _Loc(self, sel)

    def is_closed(self):
        return False


def _fill(sel, text):
    from recipe_engine import browser
    page = _Page()
    s = browser.BrowserSession(svc=None, page=page)
    out = asyncio.run(browser.op_fill(s, {"selector": sel, "text": text}))
    return out, s, page


@pytest.mark.parametrize("sel,text", [("label=Password", "hunter2"), ("css=#user", "my password is hunter2"),
                                      ("css=#pw", "hunter2")], ids=["selector", "text", "type=password"])
def test_browser_fill_refuses_on_any_of_the_three_layers(sel, text):
    out, s, page = _fill(sel, text)
    assert out.get("is_error") and "LOGIN_REQUIRED" in "".join(c["text"] for c in out["content"])
    assert s.login_seen is True
    assert page.filled == [], "the page was filled despite the refusal"


def test_browser_fill_of_an_ordinary_field_goes_through(monkeypatch):
    from recipe_engine import browser

    async def after(session, action, args, note, **kw):
        return {"content": [{"type": "text", "text": note}]}
    monkeypatch.setattr(browser, "_after_action", after)
    out, s, page = _fill("css=#user", "atg")
    assert not out.get("is_error") and page.filled == [("css=#user", "atg")]


# ------------------------------------------------------------- 5. replay fill
def test_a_replay_step_that_would_fill_a_credential_fails_before_the_fill():
    page = _Page()
    step = {"surface": "browser", "action": "fill", "selector": "css=#user", "value": "the OTP is 1234", "expect": "url=http://127.0.0.1:8765/"}
    with pytest.raises(recipes.ReplayFailed) as ei:
        asyncio.run(recipes._perform_browser(page, step, []))
    assert "credential" in ei.value.reason and page.filled == []


# ------------------------------------------------------------- 6. send-whitelist
def _saved(name, command, steps, description=""):
    r = recipes.Recipe(id=recipes.recipe_id(name), command_normalized=command, name=name, description=description,
                       surface="browser", steps=steps)
    recipes.save(r)
    return r


@pytest.mark.parametrize("word", ["send", "submit", "buy", "purchase", "pay", "post", "delete", "check out"])
def test_replay_refuses_every_destructive_word_in_the_name(word, monkeypatch):
    name = f"redteam-{word.replace(' ', '-')}"
    _saved(name, f"{word} the thing", [{"surface": "browser", "action": "goto", "value": "http://127.0.0.1:8765/", "expect": "url=http://127.0.0.1:8765/"}],
           description=f"{word} the thing")
    monkeypatch.setattr(recipes, "_replay_browser", lambda *a, **k: pytest.fail("a destructive recipe was replayed"))
    try:
        out = Engine().replay(name)
        assert out["status"] == "refused" and word.split()[0] in out["failure_reason"].lower(), out
    finally:
        recipes.forget(recipes.recipe_id(name))


def test_an_innocently_named_recipe_whose_step_targets_send_is_refused(monkeypatch):
    """The name says 'draft'; a desktop step's UIA target is the Send button."""
    name = "redteam-draft-reply"
    _saved(name, "draft a reply", [
        {"surface": "desktop", "action": "click", "input_kind": "pattern", "expect": "title=Mail",
         "uia": {"automation_id": "", "name": "Send", "control_type": "ButtonControl", "class_name": "", "ancestors": [], "pattern": "invoke", "is_password": False}},
    ])
    monkeypatch.setattr(recipes, "_replay_desktop", lambda *a, **k: pytest.fail("the Send click was replayed"))
    monkeypatch.setattr(recipes, "_replay_browser", lambda *a, **k: pytest.fail("replayed"))
    try:
        out = Engine().replay(name)
        assert out["status"] == "refused" and "send" in out["failure_reason"].lower(), out
    finally:
        recipes.forget(recipes.recipe_id(name))


def test_destructive_clicks_are_dropped_while_recording_and_reported_at_stop():
    from recipe_engine.recorder import Recorder
    rec = Recorder()
    assert rec.start("redteam-basket", "review the basket")[0]
    rec.record({"surface": "browser", "action": "goto", "value": "http://127.0.0.1:8765/", "post": {"url": "http://127.0.0.1:8765/", "title": "Shop"}, "t": time.time()})
    for sel in ("role=button[name='Buy now']", "role=button[name='Place order']", "text=Delete account"):
        rec.record({"surface": "browser", "action": "click", "selector": sel, "value": None, "post": {"url": "http://127.0.0.1:8765/", "title": "Shop"}, "t": time.time()})
    rec.record({"surface": "desktop", "action": "click", "value": None, "post": {"window": "Mail"}, "t": time.time(), "rel": [0.5, 0.5],
                "uia": {"name": "Send", "control_type": "ButtonControl", "ancestors": []}})
    assert len(rec.trace) == 1, [s.get("selector") for s in rec.trace]
    r, why = rec.stop()
    try:
        assert r is not None and len(r.steps) == 1 and "destructive step was dropped" in why
    finally:
        if r:
            recipes.forget(r.id)


def test_record_recipe_refuses_a_destructive_description():
    srv = build_server(Engine())
    out = _call(srv, "record_recipe", name="invoice-run", description="fill the invoice and submit it")
    assert out["recording"] is False and "destructive" in out["reason"]


# ------------------------------------------------------------- 7. invariant gate
def test_every_acting_tool_refuses_while_invariants_fail():
    eng = Engine()
    eng.invariant_problems = ["synthetic: safety.guard_text missing"]
    srv = build_server(eng)
    acting = ["click", "double_click", "right_click", "move", "type", "key", "scroll", "screenshot",
              "record_recipe", "replay_recipe", "browser_goto", "browser_click", "browser_fill", "browser_download"]
    args = {"click": {"x": 1, "y": 1}, "double_click": {"x": 1, "y": 1}, "right_click": {"x": 1, "y": 1}, "move": {"x": 1, "y": 1},
            "type": {"text": "hi"}, "key": {"combo": "enter"}, "scroll": {"x": 1, "y": 1, "clicks": -3}, "screenshot": {},
            "record_recipe": {"name": "x-y", "description": ""}, "replay_recipe": {"name": "x-y"},
            "browser_goto": {"url": "http://127.0.0.1:8765/"}, "browser_click": {"selector": "text=x"},
            "browser_fill": {"selector": "text=x", "text": "y"}, "browser_download": {"selector": "text=x"}}
    for tool in acting:
        out = _call(srv, tool, **args[tool])
        flat = out if isinstance(out, dict) else " ".join(map(str, out))
        text = flat.get("failure_reason", "") + flat.get("reason", "") + str(flat.get("status", "")) if isinstance(flat, dict) else flat
        assert "invariants_failed" in text or "refused" in text, (tool, out)


# ------------------------------------------------------------- 8. live WinForms
@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_live_winforms_password_box_is_refused_and_the_username_box_is_typed(monkeypatch):
    """A real password TextBox (UseSystemPasswordChar): UIA reports IsPassword, the engine
    refuses with the REAL keyboard path; the plain box next to it gets the text."""
    import ctypes
    from pathlib import Path
    from recipe_engine import display
    display.make_dpi_aware()
    import uiautomation as auto
    real_pg = desktop._pyautogui()
    seen: list[tuple] = []

    class Spy:
        FAILSAFE = real_pg.FAILSAFE
        PAUSE = real_pg.PAUSE

        def write(self, text, interval=0):
            seen.append(("write", text)); real_pg.write(text, interval=interval)

        def hotkey(self, *k):
            seen.append(("hotkey", "+".join(k))); real_pg.hotkey(*k)

        def press(self, k):
            seen.append(("press", k)); real_pg.press(k)
    monkeypatch.setattr(desktop, "_pyautogui", lambda: Spy())
    monkeypatch.setattr(desktop, "settle_and_shoot", lambda note: ActionResult(note=note))

    script = Path(__file__).parent / "fixtures" / "password_form.ps1"
    proc = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-STA", "-File", str(script)])
    win = auto.WindowControl(searchDepth=1, Name="Recipe Engine Red Team")
    try:
        assert win.Exists(15, 0.2), "the WinForms fixture did not open"
        hwnd = win.NativeWindowHandle
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        pw = auto.EditControl(searchFromControl=win, AutomationId="txtPassword")
        user = auto.EditControl(searchFromControl=win, AutomationId="txtUser")
        assert pw.Exists(5, 0.2) and user.Exists(5, 0.2)
        pw.SetFocus(); time.sleep(0.3)
        assert uia.describe_focused().is_password is True, "UIA did not flag the password box"
        with pytest.raises(safety.CredentialRefused):
            desktop.type_text("hello")
        assert seen == [], f"the real keyboard was used on a password box: {seen}"
        user.SetFocus(); time.sleep(0.3)
        assert desktop.type_text("atg").ok
        assert seen == [("write", "atg")]
        time.sleep(0.3)
        assert user.GetValuePattern().Value == "atg"
    finally:
        try:
            ctypes.windll.user32.PostMessageW(win.NativeWindowHandle, 0x0010, 0, 0)
        except Exception:
            pass
        for _ in range(30):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            proc.kill()
