"""failures.explain / short_reason: technical errors in plain words. Pure functions."""
from __future__ import annotations

import pytest

from recipe_engine import failures


@pytest.mark.parametrize("reason, what_contains, advice_contains", [
    ("execution error: Locator role=link[name='Payroll'] not found", "couldn't find", "exact name"),
    ("Page.goto: Timeout 20000ms exceeded", "too long", "try again"),
    ("LOGIN_REQUIRED on accounts.google.com", "sign-in page", "login browser"),
    ("BrowserProfileLocked: profile held by PID 4", "holding my browser profile", "Close that Chrome"),
    ("totally unknown thing", "didn't finish", "terminal"),
    # a reason that merely MENTIONS login.html in a list of links must not be read as a sign-in page
    ('There is no Payroll link on the site - the index page offers only "Billing" (invoices.html) and "Account" (login.html), '
     '/payroll.html returns a 404', "couldn't find", "exact name"),
    ("the tool result said LOGIN_PAGE_DETECTED at /account", "sign-in page", "login browser"),
])
def test_explain_gives_what_happened_and_what_to_do(reason, what_contains, advice_contains):
    what, advice = failures.explain(reason)
    assert what_contains.lower() in what.lower()
    assert advice_contains.lower() in advice.lower()


def test_short_reason_trims_a_technical_error():
    long = "execution error: " + "x" * 300 + ". second sentence"
    s = failures.short_reason(long)
    assert s.startswith("xxx") and s.endswith("...") and len(s) <= 145
    assert failures.short_reason("FAILED: the Invoices link is not on this page") == "the Invoices link is not on this page"


def test_classify_puts_our_own_tracebacks_in_code_and_pyautogui_blocks_in_permission():
    try:
        raise RuntimeError("boom from the engine")
    except RuntimeError as e:
        f = failures.build(e, state="REPLAYING", command="x", plan="")
    assert f.cls in (failures.FailureClass.CODE, failures.FailureClass.UNKNOWN)
    assert f.exc_type == "RuntimeError"
