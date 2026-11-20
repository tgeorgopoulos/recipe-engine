"""desktop.type_text: the credential hook and the one-call multi-line typing, proven
against a fake pyautogui so no real key is ever pressed."""
from __future__ import annotations

import pytest

from recipe_engine import desktop, safety, uia
from recipe_engine.screen import ActionResult


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
def fake(monkeypatch):
    pg = _FakePG()
    monkeypatch.setattr(desktop, "_pyautogui", lambda: pg)
    monkeypatch.setattr(desktop, "settle_and_shoot", lambda note: ActionResult(note=note))
    monkeypatch.setattr(desktop, "foreground_window_title", lambda: "Untitled - Notepad")
    monkeypatch.setattr(desktop, "shot_size", lambda: (1280, 720))
    monkeypatch.setattr(uia, "describe_focused", lambda: None)      # no real focus lookup in unit tests
    return pg


def test_credential_text_is_refused_before_any_key_is_pressed(fake):
    with pytest.raises(safety.CredentialRefused):
        desktop.type_text("my password is hunter2")
    assert fake.calls == [], "keys were pressed despite the refusal"


def test_newlines_become_shift_enter_in_one_action(fake):
    out = desktop.type_text("line one\nline two\n\nline four")
    assert out.ok
    assert fake.calls == [("write", "line one"), ("hotkey", "shift+enter"),
                          ("write", "line two"), ("hotkey", "shift+enter"),
                          ("hotkey", "shift+enter"),
                          ("write", "line four")]
    assert not any(c == ("write", "\n") for c in fake.calls), "a raw newline would SEND the message"


def test_empty_text_is_an_error_not_a_keypress(fake):
    assert desktop.type_text("").error == "nothing to type"
    assert fake.calls == []


def test_recorder_hook_receives_the_trace_shape(fake, monkeypatch):
    got: list[dict] = []
    monkeypatch.setattr(desktop, "_recorder_hook", got.append)
    monkeypatch.setattr(desktop, "to_physical", lambda x, y: (x, y))
    fake.click = lambda x, y: fake.calls.append(("click", x, y))
    desktop.click(640, 360)
    desktop.type_text("hello")
    desktop.key("ctrl+s")
    assert [s["action"] for s in got] == ["click", "type", "key"]
    assert got[0]["surface"] == "desktop" and got[0]["rel"] == [0.5, 0.5]
    assert got[0]["post"]["window"] == "Untitled - Notepad"
    assert got[1]["value"] == "hello" and "rel" not in got[1]
    assert got[2]["value"] == "ctrl+s"


def test_key_combo_is_split_and_lowercased(fake):
    desktop.key("Ctrl + S")
    assert fake.calls == [("hotkey", "ctrl+s")]
    desktop.key("enter")
    assert fake.calls[-1] == ("press", "enter")
