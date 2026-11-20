"""take_over_profile: graceful-close-then-force (V4.2).

IMPORTANT, measured finding (not assumed): a graceful Windows close request
(`taskkill /PID`, which posts WM_CLOSE - the same signal the window's own X
button sends) does NOT reliably make Chrome flush a just-set cookie to its
on-disk store. A live diagnostic (real Chrome, real cookie, graceful close,
polled for 30s after exit) never saw it appear. So this function is NOT a
login-persistence mechanism and must never be treated as one - the tested,
actual mechanism is session_store.py, which reads the live cookie jar over
CDP from the browser this process is driving and needs no disk flush at all.

What this function DOES guarantee, and what these tests check:
  1. it tries a clean close first (best effort; harmless if it does nothing),
  2. anything still alive after the grace window is force-killed, so the
     original bound - the profile is free within `wait_s` no matter what - holds.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from recipe_engine import config


def test_still_force_kills_a_process_that_ignores_the_close_request(monkeypatch):
    """A hung / unresponsive Chrome must still be closed within a bounded time
    (the original guarantee), not left holding the profile forever."""
    from recipe_engine import browser

    monkeypatch.setattr(config, "BROWSER_GRACEFUL_CLOSE_S", 1.0)
    calls = []

    def fake_pids(profile_dir=None):
        return [] if calls.count("killed") else [999999]   # "alive" until force-killed

    def fake_run(cmd, **kw):
        from types import SimpleNamespace
        calls.append("killed" if "/F" in cmd else "graceful")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(browser, "profile_chrome_pids", fake_pids)
    monkeypatch.setattr(browser.subprocess, "run", fake_run)
    monkeypatch.setattr(browser.sys, "platform", "win32")
    n = browser.take_over_profile(Path("x"), wait_s=3.0, graceful_s=0.5)
    assert n == 1 and calls == ["graceful", "killed"], calls


def test_a_process_that_closes_gracefully_is_not_also_force_killed(monkeypatch):
    """If the graceful request works, take_over_profile must not needlessly force-kill
    anything (no /T /F on a process that already exited cleanly on its own)."""
    from recipe_engine import browser

    calls = []

    def fake_pids(profile_dir=None):
        return [] if calls else [111]        # gone after the one graceful call

    def fake_run(cmd, **kw):
        from types import SimpleNamespace
        calls.append("/F" in cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(browser, "profile_chrome_pids", fake_pids)
    monkeypatch.setattr(browser.subprocess, "run", fake_run)
    monkeypatch.setattr(browser.sys, "platform", "win32")
    n = browser.take_over_profile(Path("x"), wait_s=3.0, graceful_s=2.0)
    assert n == 1 and calls == [False], f"force-killed a process that already closed cleanly: {calls}"


@pytest.mark.slow
@pytest.mark.skipif(sys.platform != "win32", reason="graceful-close path is Windows-specific (taskkill)")
def test_profile_is_freed_within_the_bound_against_a_real_chrome(tmp_path):
    """End-to-end against a real Chrome process (not Playwright-driven - the actual
    gap this exists for): take_over_profile must return, and the profile must be
    free, well within its stated bound."""
    from recipe_engine import browser

    profile = tmp_path / "external-profile"
    proc = subprocess.Popen([str(config.CHROME_PATH), f"--user-data-dir={profile}", "--no-first-run",
                            "--no-default-browser-check", "about:blank"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not browser.profile_chrome_pids(profile):
            time.sleep(0.2)
        assert browser.profile_chrome_pids(profile), "the external Chrome was never detected on the profile"
        t0 = time.time()
        n = browser.take_over_profile(profile, wait_s=10.0, graceful_s=3.0)
        elapsed = time.time() - t0
        assert n > 0 and not browser.profile_chrome_pids(profile)
        assert elapsed < 13.0 + 5.0, f"took {elapsed:.1f}s, longer than the graceful+wait bound allows"
    finally:
        if proc.poll() is None:
            proc.kill()
