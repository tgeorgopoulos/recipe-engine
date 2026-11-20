"""The safety gates every other module imports from - ONE place, so a change here
is a change everywhere and the invariant self-test can pin it.

* `_SECRET_RE`: the credential tripwire. It matches the WORDS that mean "this is a
  secret" (password, OTP, 2FA ...), not secret values - it exists so a workflow
  can never type "enter your password" style content, and so a recipe that would
  is refused before any key is pressed.
* `guard_text()`: raise `CredentialRefused` for text (or a selector) that trips it.
  Called at the top of every desktop `type` and browser `fill` path.
* `is_destructive()`: the send-whitelist. Anything that sends, submits, buys, pays,
  posts, deletes ... is refused for replay and never recorded (config.DESTRUCTIVE_WORDS).

Pure functions, no I/O, no model calls.
"""
from __future__ import annotations

import re
from typing import Any

from recipe_engine import config

# The single credential regex. Lifted verbatim from the original executor.py:227.
_SECRET_RE = re.compile(r"(password|passcode|passwd|one[- ]time|verification code|2fa|mfa|otp|pin\b)", re.I)

# Field NAMES (UIA Name / accessible name) that identify a secret field even when the
# control is not flagged IsPassword - card security codes included.
SECRET_FIELD_NAME_RE = re.compile(
    r"(password|passcode|passwd|otp|one[- ]time|verification|2fa|mfa|cvv|cvc|security code|\bpin\b)", re.I)

_DESTRUCTIVE_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in config.DESTRUCTIVE_WORDS) + r")\b", re.I)


class CredentialRefused(Exception):
    """Text or a target that looks like a credential. Nothing was typed."""


def guard_text(text: str, selector: str = "") -> None:
    """Refuse to type/fill credential-looking content. Raises CredentialRefused.
    Checks the text AND the selector (a selector like `label=Password` is as telling
    as the text), mirroring the three-layer gate in browser.op_fill."""
    if _SECRET_RE.search(str(text or "")) or _SECRET_RE.search(str(selector or "")):
        raise CredentialRefused("Typing credentials or codes is forbidden.")


def element_is_secret(target: Any) -> str:
    """Why a UIA target must never receive typed text: '' if it is fine. Checks the
    control's IsPassword flag first, then its accessible name / AutomationId and
    the name of the nearest labelled ancestors (a text box inside a group called
    'Sign in - Password')."""
    if target is None:
        return ""
    if bool(getattr(target, "is_password", False)):
        return "the focused control is a password field (IsPassword)"
    for attr in ("name", "automation_id"):
        v = str(getattr(target, attr, "") or "")
        if v and SECRET_FIELD_NAME_RE.search(v):
            return f"the focused control is named {v!r}"
    anc = list(getattr(target, "ancestors", None) or [])
    for a in anc[-2:]:                                      # the direct parent and its parent
        v = str((a or {}).get("name") or "")
        if v and SECRET_FIELD_NAME_RE.search(v):
            return f"the focused control sits inside {v!r}"
    return ""


def guard_focused_element(target: Any = None) -> None:
    """Refuse to type into a password / code field, whatever the text is. `target` is
    a uia.UiaTarget (or None to look the focused control up now). If UI Automation
    cannot describe the focus at all, typing proceeds - the text gate still applies."""
    if target is None:
        from recipe_engine import uia
        target = uia.describe_focused()
    why = element_is_secret(target)
    if why:
        raise CredentialRefused(f"Typing here is forbidden: {why}.")


def is_destructive(command: str, plan: str = "", risk: str = "") -> bool:
    """True if the text names an irreversible action (send, submit, pay, delete, ...).
    `risk` is kept for callers that carry an explicit classification."""
    if (risk or "").strip().lower().startswith("destructive"):
        return True
    return bool(_DESTRUCTIVE_RE.search(command or "") or _DESTRUCTIVE_RE.search(plan or ""))


def destructive_reason(command: str, plan: str = "", risk: str = "") -> str:
    if (risk or "").strip().lower().startswith("destructive"):
        return "marked destructive"
    m = _DESTRUCTIVE_RE.search(command or "") or _DESTRUCTIVE_RE.search(plan or "")
    return f"matched destructive word {m.group(1)!r}" if m else ""


def _selftest() -> None:
    assert _SECRET_RE.search("my password is") and not _SECRET_RE.search("hello world")
    assert _SECRET_RE.search("PIN") and not _SECRET_RE.search("pinned tab")
    for bad in ("enter your password", "the OTP code", "2FA token"):
        try:
            guard_text(bad)
        except CredentialRefused:
            pass
        else:
            raise AssertionError(f"guard_text let through {bad!r}")
    try:
        guard_text("hello", "label=Password")
    except CredentialRefused:
        pass
    else:
        raise AssertionError("selector was not checked")
    guard_text("hours for week 12")
    from types import SimpleNamespace as _T
    assert element_is_secret(_T(is_password=True, name="", automation_id="", ancestors=[]))
    assert element_is_secret(_T(is_password=False, name="Confirm password", automation_id="", ancestors=[]))
    assert element_is_secret(_T(is_password=False, name="", automation_id="txtCVV", ancestors=[]))
    assert not element_is_secret(_T(is_password=False, name="Text editor", automation_id="", ancestors=[{"name": "Untitled - Notepad"}]))
    assert not element_is_secret(None)
    assert is_destructive("send the invoice") and is_destructive("check out now")
    assert not is_destructive("open notepad and type hello")
    assert destructive_reason("submit the form") == "matched destructive word 'submit'"
    print("safety self-test ok")


if __name__ == "__main__":
    _selftest()
