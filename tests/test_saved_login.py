"""Saved login (V4.1): session cookies survive a browser restart, and the login
detector lets an SSO auto-redirect finish instead of stopping on the hop.

A tiny local site stands in for a vendor portal + SSO:
  /login          a real sign-in form (password field)              -> IS a login page
  /do-login       sets a SESSION cookie sid=ok and goes to /home
  /home           "Welcome back" when sid=ok, else redirects to /login
  /login/sso      looks like a login URL, but redirects to /home after ~1 s
                  when sid=ok (the Microsoft "keep me signed in" hop)   -> NOT a login page
Everything runs on a TEMP profile; the real agent profile is never touched.
"""
from __future__ import annotations

import http.server
import tempfile
import threading
import time
from pathlib import Path

import pytest

from recipe_engine import config
from recipe_engine import session_store

pytestmark = pytest.mark.slow           # launches real Chrome twice (~6 s)


class _Site(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _page(self, code, body, extra=None):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        signed = "sid=ok" in (self.headers.get("Cookie") or "")
        p = self.path.split("?")[0]
        if p == "/login":
            self._page(200, "<h1>Sign in</h1><form><input name=u><input type=password name=p><button>Go</button></form>")
        elif p == "/do-login":
            self._page(302, "", {"Location": "/home", "Set-Cookie": "sid=ok; Path=/; HttpOnly"})   # SESSION cookie: no Expires
        elif p == "/home":
            if signed:
                self._page(200, "<h1>Welcome back</h1><p>dashboard</p><a href='/logout'>Log out</a>")
            else:
                self._page(302, "", {"Location": "/login"})
        elif p == "/login/sso":
            if signed:
                self._page(200, "<p>Signing you in...</p><script>setTimeout(()=>location.replace('/home'),900)</script>")
            else:
                self._page(302, "", {"Location": "/login"})
        else:
            self._page(404, "nope")


@pytest.fixture(scope="module")
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def temp_profile(monkeypatch, tmp_path):
    """Point the browser service at a throwaway profile + snapshot file."""
    from recipe_engine import browser
    prof = tmp_path / "profile"
    monkeypatch.setattr(config, "BROWSER_PROFILE_DIR", prof)
    monkeypatch.setattr(config, "SESSION_STATE_FILE", tmp_path / "session.bin")
    monkeypatch.setattr(config, "BROWSER_HEADLESS", True)
    monkeypatch.setattr(config, "SESSION_SNAPSHOT_S", 1)
    monkeypatch.setattr(config, "BROWSER_TAKE_OVER_PROFILE", False)
    browser.close_service()
    yield prof
    browser.close_service()


def _run(coro):
    from recipe_engine import browser
    return browser.service().run(coro, timeout=60)


def test_session_cookie_survives_a_restart(site, temp_profile):
    from recipe_engine import browser

    async def sign_in():
        page = await browser.service().new_page()
        await page.goto(site + "/do-login", wait_until="domcontentloaded")
        assert "Welcome back" in await page.content()
        await browser.service().snapshot_session()          # what the 5 s timer does
        return await browser.service()._ctx.cookies()
    cookies = _run(sign_in())
    assert any(c["name"] == "sid" and c["expires"] == -1 for c in cookies), "the test cookie must be a SESSION cookie"
    time.sleep(0.5)                                        # the on-load hook runs on the service loop
    ok, words = session_store.describe(site)
    assert ok and "signed in at 127.0.0.1" in words, words   # the Log out link on /home confirmed the sign-in
    assert session_store.hosts_with_logins() == ["127.0.0.1"]

    browser.close_service()                                # Chrome exits: session cookies would normally be gone

    async def come_back():
        page = await browser.service().new_page()          # fresh launch on the same profile + restored snapshot
        await page.goto(site + "/home", wait_until="domcontentloaded")
        return page.url, await page.content()
    url, html = _run(come_back())
    assert url.endswith("/home") and "Welcome back" in html, f"session was lost across the restart: {url}"


def test_detector_waits_for_an_sso_redirect_but_still_catches_a_real_form(site, temp_profile):
    from recipe_engine import browser

    async def go():
        svc = browser.service()
        page = await svc.new_page()
        await page.goto(site + "/do-login", wait_until="domcontentloaded")
        # the SSO hop: login-looking URL that finishes signing in by itself
        await page.goto(site + "/login/sso", wait_until="domcontentloaded")
        t0 = time.time()
        hop = await browser.detect_login_page(page)
        settled = time.time() - t0
        # a real sign-in form: must be caught, quickly, even though the URL is the same family
        await svc._ctx.clear_cookies()
        await page.goto(site + "/login", wait_until="domcontentloaded")
        real = await browser.detect_login_page(page)
        # a login URL that never resolves and shows no form: still a login page after the settle window
        await page.goto(site + "/login/sso", wait_until="domcontentloaded")   # no sid -> 302 -> /login (form)
        stuck = await browser.detect_login_page(page)
        return hop, settled, real, stuck, page.url
    hop, settled, real, stuck, url = _run(go())
    assert hop is None, f"the SSO hop was flagged as a login page: {hop}"
    assert settled < config.LOGIN_SETTLE_MS / 1000 + 2
    assert real and "password" in real, real
    assert stuck and ("password" in stuck or "login" in stuck), (stuck, url)


def test_snapshot_is_sealed_and_describes_sites(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_STATE_FILE", tmp_path / "s.bin")
    state = {"cookies": [
        {"name": "portal_session", "value": "v", "domain": "acme.vendorportal.com", "path": "/", "expires": -1,
         "httpOnly": True, "secure": True, "sameSite": "Lax"},
        {"name": "ESTSAUTHPERSISTENT", "value": "v", "domain": ".login.microsoftonline.com", "path": "/", "expires": 2e9,
         "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "_ga", "value": "v", "domain": ".acme.com", "path": "/", "expires": 2e9}], "origins": []}
    assert session_store.save(state)
    raw = (tmp_path / "s.bin").read_bytes()
    assert b"portal_session" not in raw or not raw.startswith(b"{"), "session tokens must not sit in plaintext"
    assert session_store.load()["cookies"][0]["name"] == "portal_session"
    # a cookie is NOT a login (some portals give anonymous visitors a portal_session too)
    ok, words = session_store.describe("https://acme.vendorportal.com/invoices")
    assert ok is False and "never saw you signed in" in words, words
    assert session_store.describe("https://portal.acme.com/")[1].startswith("I have no saved login")
    assert session_store.hosts_with_logins() == []
    # a confirmed sign-in at the user's real portal host is what the planner and the retry use
    session_store.mark_verified("https://portal.acme.com/invoices")
    assert session_store.describe("portal.acme.com")[0] is True
    assert session_store.hosts_with_logins() == ["portal.acme.com"]
    assert session_store.alternative_host("https://acme.vendorportal.com/login/sso") == "portal.acme.com"
    assert session_store.alternative_host("https://login.portal.acme.com/") == "portal.acme.com"
    assert session_store.alternative_host("https://accounts.google.com/") is None
    session_store.save(state)                                # a cookie refresh must keep the confirmation
    assert session_store.hosts_with_logins() == ["portal.acme.com"]
    assert session_store.forget() and session_store.load() is None
