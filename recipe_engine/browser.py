"""
Browser surface (V3): Playwright on the SAME persistent Chrome profile that the
user signed into by hand (config.BROWSER_PROFILE_DIR).

Design
------
* ONE BrowserService per process. It owns a dedicated thread running an asyncio
  loop, and on that loop exactly one `launch_persistent_context()` (real Chrome
  channel, headed). A persistent profile can back only one context per process,
  so parallel tasks each get their own PAGE (tab) inside it; every tab shares the
  live cookies/storage, so logins work everywhere.
* Playwright objects must be used from the loop that created them, so every
  operation is a coroutine submitted to the service loop. Tool handlers running
  inside the executor's own loop marshal with `await service.call(coro)`.
* Actions are SEMANTIC: role / text / label / placeholder / test-id / css
  selectors, never coordinates. That is what makes browser recipes durable.
* Login / 2FA: the model is told to stop and reply LOGIN_REQUIRED (same rule as
  the desktop prompt) AND detect_login_page() runs after every action in code.
  On a hit the tool result says LOGIN_PAGE_DETECTED and `session.login_seen`
  is set, which forbids writing a recipe for the run. Credentials are never
  filled: `fill` refuses anything _SECRET_RE matches and any password field.
* Profile ownership: a stray chrome.exe already holding the profile blocks the
  launch. With BROWSER_TAKE_OVER_PROFILE only processes whose command line
  names that exact directory are terminated (never a bare "kill chrome").

verify() never touches the real profile (it uses a temp dir), so the
self-repair verifier cannot kill the agent's own browser.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from recipe_engine import config
from recipe_engine.config import log

# Same credential gate as the desktop tools (single source of truth in safety.py).
from recipe_engine.safety import _SECRET_RE

_LOGIN_URL_RE = re.compile(config.LOGIN_URL_RE, re.I)
_LOGIN_TEXT_RE = re.compile(config.LOGIN_TEXT_RE, re.I)


def _keep_session_cookies_on_exit(profile: Path) -> None:
    """V4.1: Chrome deletes SESSION cookies (SSO portals) on its next launch unless the
    profile is set to "continue where you left off" (session.restore_on_startup = 1).
    Set it before every launch so a clean exit keeps the user signed in. Best effort;
    the sealed snapshot (session_store) covers the cases this cannot (a killed Chrome)."""
    try:
        pref = profile / "Default" / "Preferences"
        data: dict = {}
        if pref.exists():
            data = json.loads(pref.read_text(encoding="utf-8", errors="replace") or "{}")
        if data.get("session", {}).get("restore_on_startup") == 1:
            return
        data.setdefault("session", {})["restore_on_startup"] = 1
        pref.parent.mkdir(parents=True, exist_ok=True)
        pref.write_text(json.dumps(data), encoding="utf-8")
        log("BROWSER", "profile set to keep session cookies across restarts (restore_on_startup=1)")
    except Exception as e:
        log("BROWSER", f"could not set restore_on_startup on the profile: {e!r}")


class BrowserProfileLocked(Exception):
    """The persistent profile is held by a Chrome we may not / could not close."""


class BrowserUnavailable(Exception):
    """Playwright or the Chrome channel is not usable."""


# ---------------------------------------------------------------------------
# Profile ownership helpers
# ---------------------------------------------------------------------------
def profile_chrome_pids(profile_dir: Path | None = None) -> list[int]:
    """PIDs of chrome processes whose command line names the agent profile dir."""
    d = str(profile_dir or config.BROWSER_PROFILE_DIR)
    try:
        if sys.platform == "win32":
            ps = ("Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
                  "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30)
            out = r.stdout.strip()
            if not out:
                return []
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            key = d.lower().replace("/", "\\")
            return [int(p["ProcessId"]) for p in data
                    if p.get("CommandLine") and key in str(p["CommandLine"]).lower().replace("/", "\\")]
        r = subprocess.run(["pgrep", "-f", d], capture_output=True, text=True, timeout=10)
        return [int(x) for x in r.stdout.split() if x.isdigit()]
    except Exception as e:
        log("BROWSER", f"could not list chrome processes: {e!r}")
        return []


def take_over_profile(profile_dir: Path | None = None, wait_s: float = 10.0,
                      graceful_s: float = config.BROWSER_GRACEFUL_CLOSE_S) -> int:
    """Terminate the chrome processes holding the agent profile. Returns how many.

    V4.2: graceful close attempted first, force only what survives. This gives a
    responsive Chrome a bounded window to exit cleanly (fewer orphaned temp/lock
    files, no different in outcome otherwise) before falling back to the original
    unconditional `taskkill /F`, with the same worst-case bound as before.

    MEASURED, not assumed (tests/test_profile_takeover.py): a graceful close does
    NOT reliably flush a just-set cookie to Chrome's on-disk store, even 30+
    seconds later - so this function is NOT how a login gets saved, and it must
    never be treated as one. The actual, tested persistence mechanism is
    session_store.py: it pulls the live cookie jar over CDP from the browser THIS
    process is driving (session_snapshot loop + on every page load + before this
    process's own clean close), which needs no disk flush from Chrome at all. That
    is what protects a real sign-in; this function only bounds shutdown time for
    whatever Chrome happens to be in the way when the profile is taken over."""
    pids = profile_chrome_pids(profile_dir)
    if not pids:
        return 0
    log("BROWSER", f"taking over the agent profile: closing chrome PIDs {pids} (graceful first, "
                   f"then forcing anything still alive after {graceful_s:.0f}s)")
    for pid in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, timeout=graceful_s + 2)
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass          # not every PID owns a closable window (renderers, GPU process, ...) - expected
    grace_deadline = time.time() + graceful_s
    while time.time() < grace_deadline and profile_chrome_pids(profile_dir):
        time.sleep(0.2)
    survivors = profile_chrome_pids(profile_dir)
    if survivors:
        log("BROWSER", f"{len(survivors)} process(es) did not close gracefully within {graceful_s:.0f}s; forcing")
        for pid in survivors:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
                else:
                    os.kill(pid, signal.SIGKILL)
            except Exception as e:
                log("BROWSER", f"force kill {pid} failed: {e!r}")
    else:
        log("BROWSER", "all chrome processes closed cleanly")
    deadline = time.time() + wait_s
    while time.time() < deadline and profile_chrome_pids(profile_dir):
        time.sleep(0.2)          # wait-until, bounded
    left = profile_chrome_pids(profile_dir)
    if left:
        raise BrowserProfileLocked(f"chrome still holds the agent profile (PIDs {left})")
    return len(pids)


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------
_PREFIXED = ("label=", "placeholder=", "testid=", "test-id=", "alt=", "title=")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def resolve_locator(page, selector: str):
    """Map our selector strings onto Playwright locators.
       role=button[name='Save']   text=Invoices   css=#id   xpath=//a    (native engines, passed through)
       label=Search   placeholder=Email   testid=submit   alt=Logo   title=Close   (getBy* helpers)
       anything else -> page.locator(selector)"""
    s = (selector or "").strip()
    if not s:
        raise ValueError("empty selector")
    low = s.lower()
    if low.startswith("label="):
        return page.get_by_label(_unquote(s[6:]))
    if low.startswith("placeholder="):
        return page.get_by_placeholder(_unquote(s[12:]))
    if low.startswith("testid="):
        return page.get_by_test_id(_unquote(s[7:]))
    if low.startswith("test-id="):
        return page.get_by_test_id(_unquote(s[8:]))
    if low.startswith("alt="):
        return page.get_by_alt_text(_unquote(s[4:]))
    if low.startswith("title="):
        return page.get_by_title(_unquote(s[6:]))
    return page.locator(s)


def is_semantic_selector(selector: str) -> bool:
    low = (selector or "").strip().lower()
    return low.startswith(("role=", "text=", "label=", "placeholder=", "testid=", "test-id=", "alt=", "title="))


# ---------------------------------------------------------------------------
# Login detection (code-level; the prompt rule is the other half)
# ---------------------------------------------------------------------------
_SIGNED_IN_RE = re.compile(r"\b(log ?out|sign ?out|logout|signout|cerrar sesi[oó]n|d[ée]connexion)\b", re.I)


async def _signed_in_marker(page) -> bool:
    """A Log out / Sign out link or button: the page is being shown to a signed-in user."""
    try:
        return await page.locator("a, button, [role=menuitem], [role=button]", has_text=_SIGNED_IN_RE).count() > 0
    except Exception:
        return False


async def _credential_field_visible(page) -> bool:
    """A password or one-time-code box the user would have to fill: a real sign-in page."""
    try:
        if await page.locator("input[type=password]:visible").count() > 0:
            return True
        return await page.locator("input[autocomplete='one-time-code']:visible, input[name*=otp i]:visible, "
                                  "input[id*=otp i]:visible").count() > 0
    except Exception:
        return False


async def detect_login_page(page, settle: bool = True) -> Optional[str]:
    """Reason string if `page` looks like a login / 2FA challenge, else None.

    V4.1: a login-looking URL is not a verdict by itself. With a saved session,
    portal -> login.microsoftonline.com -> portal completes on its own in a
    second or two, and the old detector fired on the intermediate hop and
    stopped a sign-in that was about to succeed. So when the URL matches, wait
    (LOGIN_SETTLE_MS) for the redirects to finish: a credential field appearing
    at any point is a login page; landing on a non-login URL is not; a login URL
    that stays put for LOGIN_QUIET_MS without a form (an account picker, an
    error, a plain sign-in button) is one too."""
    try:
        url = page.url or ""
        if _LOGIN_URL_RE.search(url):
            if not settle:
                return f"url matches a login pattern ({url[:120]})"
            deadline = time.time() + config.LOGIN_SETTLE_MS / 1000
            last, quiet_since = url, time.time()
            while True:
                if await _credential_field_visible(page):
                    return "visible password field"
                cur = page.url or ""
                if not _LOGIN_URL_RE.search(cur):
                    log("BROWSER", f"login-looking URL redirected on its own -> {cur[:100]} (saved session worked)")
                    try:
                        from recipe_engine import session_store
                        session_store.mark_verified(cur)
                    except Exception:
                        pass
                    url = cur
                    break
                if cur != last:
                    last, quiet_since = cur, time.time()
                elif time.time() - quiet_since >= config.LOGIN_QUIET_MS / 1000 or time.time() >= deadline:
                    return f"url matches a login pattern and did not redirect ({cur[:120]})"
                await asyncio.sleep(config.LOGIN_SETTLE_POLL_MS / 1000)
        if await _credential_field_visible(page):
            return "visible password field"
        title = await page.title()
        if _LOGIN_TEXT_RE.search(title or ""):
            return f"title {title[:80]!r}"
        body = await page.locator("body").inner_text(timeout=2000)
        m = _LOGIN_TEXT_RE.search(body[:20000])
        if m:
            return f"page text {m.group(0)!r}"
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# The service (one per process)
# ---------------------------------------------------------------------------
class BrowserService:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._pw = None
        self._ctx = None
        self._closed_event: Optional[asyncio.Event] = None
        self._task_pages: list = []          # pages handed to tasks, oldest first
        self._keep: set = set()              # pages that must stay open (login, failure)
        self.launches = 0
        self._temp_profile = False           # verify() runs on a throwaway dir: never touch the saved login
        self._snapshot_task = None           # V4.1: periodic cookie-jar snapshot (session_store)
        self._login_seen_pages: set = set()  # id(page) of tabs that showed a login URL (sign-in in progress)

    # ----------------------------------------------------------- lifecycle
    def start(self) -> "BrowserService":
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._ready.clear()
            self._thread = threading.Thread(target=self._run_loop, name="browser-loop", daemon=True)
            self._thread.start()
            self._ready.wait(10)
        return self

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        self.start()
        assert self._loop is not None
        return self._loop

    def submit(self, coro: Awaitable) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro: Awaitable, timeout: Optional[float] = None):
        """Blocking call from any thread OTHER than the browser loop."""
        return self.submit(coro).result(timeout)

    async def call(self, coro: Awaitable):
        """Await from a foreign asyncio loop (the executor's)."""
        return await asyncio.wrap_future(self.submit(coro))

    @property
    def started(self) -> bool:
        return self._ctx is not None

    # -------------------------------------------------------------- context
    async def ensure_context(self, profile_dir: Path | None = None, headless: Optional[bool] = None):
        if self._ctx is not None:
            return self._ctx
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise BrowserUnavailable("playwright is not installed (pip install playwright && playwright install chromium)") from e
        profile = Path(profile_dir or config.BROWSER_PROFILE_DIR)
        profile.mkdir(parents=True, exist_ok=True)
        self._temp_profile = profile_dir is not None
        if profile_dir is None:                     # the REAL profile: make sure we own it
            pids = profile_chrome_pids(profile)
            if pids:
                if not config.BROWSER_TAKE_OVER_PROFILE:
                    raise BrowserProfileLocked(f"Chrome is already open on the agent profile (PIDs {pids}); close it first")
                take_over_profile(profile)
        if self._pw is None:
            self._pw = await async_playwright().start()
        _keep_session_cookies_on_exit(profile)
        t0 = time.time()
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel=config.BROWSER_CHANNEL,
                headless=config.BROWSER_HEADLESS if headless is None else headless,
                no_viewport=True,
                args=["--start-maximized"],
                accept_downloads=True,          # V4.3: op_download saves to config.BROWSER_DOWNLOAD_DIR
                downloads_path=str(config.BROWSER_DOWNLOAD_DIR),   # stage where WE can reach it, not a temp
                                                                   # dir Chrome's own manager empties on exit
                timeout=config.BROWSER_LAUNCH_TIMEOUT_S * 1000,
            )
        except Exception as e:
            msg = str(e)
            if "in use" in msg.lower() or "already running" in msg.lower() or "singleton" in msg.lower():
                raise BrowserProfileLocked(f"profile locked: {msg[:200]}") from e
            raise BrowserUnavailable(f"could not launch Chrome ({config.BROWSER_CHANNEL}): {msg[:300]}") from e
        self.launches += 1
        self._ctx.set_default_timeout(config.BROWSER_ACTION_TIMEOUT_MS)
        self._ctx.set_default_navigation_timeout(config.BROWSER_NAV_TIMEOUT_MS)
        self._closed_event = asyncio.Event()
        self._ctx.on("close", lambda *_: self._on_context_closed())
        log("BROWSER", f"persistent context up on {profile} ({config.BROWSER_CHANNEL}, "
                       f"{'headless' if (config.BROWSER_HEADLESS if headless is None else headless) else 'headed'}) "
                       f"in {time.time() - t0:.1f}s")
        if config.SESSION_RESTORE and not self._temp_profile:
            await self._restore_session()
            self._snapshot_task = asyncio.create_task(self._snapshot_loop())
        return self._ctx

    # ------------------------------------------------- V4.1: saved login
    async def _restore_session(self) -> None:
        """Put the last snapshot's cookies back (session cookies included) right
        after launch, so a fresh Chrome starts signed in to whatever the user
        signed into last time. Cookies Chrome already has are simply overwritten."""
        from recipe_engine import session_store
        snap = session_store.load()
        cookies = list((snap or {}).get("cookies") or [])
        if not cookies:
            return
        allowed = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
        clean = [{k: c[k] for k in allowed if k in c} for c in cookies if c.get("domain")]
        try:
            await self._ctx.add_cookies(clean)
            added = len(clean)
        except Exception:                                   # one bad cookie must not lose the rest
            added = 0
            for c in clean:
                try:
                    await self._ctx.add_cookies([c])
                    added += 1
                except Exception:
                    pass
        hosts = session_store.hosts_with_logins(snap)
        log("BROWSER", f"restored {added} saved cookie(s); logins for: {', '.join(hosts) if hosts else 'none'}")

    async def snapshot_session(self) -> bool:
        """Write the live cookie jar to the sealed snapshot. Never raises."""
        from recipe_engine import session_store
        try:
            if self._ctx is None:
                return False
            state = await self._ctx.storage_state()
        except Exception as e:
            log("BROWSER", f"session snapshot skipped: {e!r}")
            return False
        return session_store.save(state) is not None

    async def _snapshot_loop(self) -> None:
        try:
            while self._ctx is not None:
                await asyncio.sleep(config.SESSION_SNAPSHOT_S)
                await self.snapshot_session()
        except asyncio.CancelledError:
            pass

    def _on_context_closed(self) -> None:
        log("BROWSER", "browser context closed (window closed by the user?)")
        self._ctx = None
        self._task_pages.clear()
        self._keep.clear()
        if self._closed_event is not None:
            self._closed_event.set()

    async def wait_closed(self) -> None:
        """Resolve when the user closes the browser window (used by --login-browser)."""
        if self._ctx is None or self._closed_event is None:
            return
        await self._closed_event.wait()

    async def new_page(self):
        """A fresh tab for a task. Old finished tabs beyond BROWSER_KEEP_PAGES are closed."""
        ctx = await self.ensure_context()
        live = [p for p in self._task_pages if not p.is_closed()]
        while len(live) >= max(0, config.BROWSER_KEEP_PAGES):
            victim = next((p for p in live if p not in self._keep), None)
            if victim is None:
                break
            try:
                await victim.close()
            except Exception:
                pass
            live.remove(victim)
        self._task_pages = live
        # Reuse Chrome's initial blank tab the first time instead of leaving it dangling.
        page = None
        for p in ctx.pages:
            if p not in self._task_pages and p.url in ("about:blank", "chrome://newtab/", "chrome://new-tab-page/") and not p.is_closed():
                page = p
                break
        if page is None:
            page = await ctx.new_page()
        self._task_pages.append(page)
        if config.SESSION_RESTORE and not self._temp_profile:
            # A sign-in that finishes right before the user closes the window must not be lost to the timer,
            # and a page that shows a Log out control is proof the user IS signed in there.
            page.on("load", lambda *_: asyncio.ensure_future(self._on_page_loaded(page)))
        return page

    async def _on_page_loaded(self, page) -> None:
        """Two signals that the user IS signed in at the page's host, neither of which
        needs the DOM to say "Log out" (some portals hide it in a menu):
          * this tab was on a login-looking URL earlier and now shows a non-login page
            (the user, or SSO, just completed a sign-in: login.portal.acme.com -> Microsoft
            -> portal.acme.com dashboard), or
          * the page shows a Log out / Sign out control anyway."""
        try:
            url = page.url or ""
            key = id(page)
            if not url.startswith("http"):
                return
            if _LOGIN_URL_RE.search(url):
                self._login_seen_pages.add(key)
            else:
                completed = key in self._login_seen_pages
                if completed or await _signed_in_marker(page):
                    from recipe_engine import session_store
                    session_store.mark_verified(url)
                    self._login_seen_pages.discard(key)
        except Exception:
            pass
        await self.snapshot_session()

    def keep_page(self, page) -> None:
        """Never auto-close this page (login page waiting for the user, failed step to inspect)."""
        self._keep.add(page)

    async def _close(self) -> None:
        if self._snapshot_task is not None:
            self._snapshot_task.cancel()
            self._snapshot_task = None
        if self._ctx is not None and not self._temp_profile and config.SESSION_RESTORE:
            await self.snapshot_session()            # the last word on what the user is signed into
        try:
            if self._ctx is not None:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._ctx = None
        self._pw = None

    def close(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            return
        try:
            self.run(self._close(), timeout=15)
        except Exception as e:
            log("BROWSER", f"close: {e!r}")
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self._thread = None


_service: Optional[BrowserService] = None
_service_lock = threading.Lock()


def service() -> BrowserService:
    global _service
    with _service_lock:
        if _service is None:
            _service = BrowserService()
        return _service.start()


def close_service() -> None:
    """Shut the browser down at agent exit (no-op if it was never started)."""
    global _service
    with _service_lock:
        svc, _service = _service, None
    if svc is not None:
        svc.close()


def is_running() -> bool:
    return _service is not None and _service.started


# ---------------------------------------------------------------------------
# Convenience entry points used outside the executor
# ---------------------------------------------------------------------------
def open_url(url: str, timeout: float = 90.0):
    """Open `url` in a new tab of the persistent context (desktop `open_browser`
    tool and --login-browser both come here so the profile has ONE owner)."""
    url = config.normalize_url(url)
    svc = service()

    async def _go():
        page = await svc.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        svc.keep_page(page)
        return page.url

    return svc.run(_go(), timeout=timeout)


def wait_until_closed() -> None:
    svc = service()
    svc.run(svc.wait_closed())


# ---------------------------------------------------------------------------
# A task's browser session: its page, trace and login flag
# ---------------------------------------------------------------------------
@dataclass
class BrowserSession:
    svc: BrowserService
    page: Any = None
    login_seen: bool = False
    login_reason: str = ""
    trace: list[dict] = field(default_factory=list)
    actions: int = 0

    async def ensure_page(self):
        if self.page is None or self.page.is_closed():
            self.page = await self.svc.new_page()
        return self.page

    def record(self, action: str, selector: Optional[str], value: Optional[str], url: str, title: str) -> None:
        self.trace.append({"surface": "browser", "action": action, "selector": selector, "value": value,
                           "post": {"url": url, "title": title}, "t": time.time()})


def _text(s: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": s}]}


def _err(s: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": s}], "is_error": True}


async def aria_snapshot(page, max_chars: Optional[int] = None) -> str:
    cap = max_chars or config.BROWSER_SNAPSHOT_MAX_CHARS
    try:
        snap = await page.locator("body").aria_snapshot()
    except Exception as e:
        return f"(snapshot unavailable: {e!r})"
    if len(snap) > cap:
        snap = snap[:cap] + f"\n... (truncated, {len(snap) - cap} more chars; use read(selector) or scroll)"
    return snap or "(empty page)"


async def _after_action(session: BrowserSession, action: str, args: dict, note: str,
                        selector: Optional[str] = None, value: Optional[str] = None,
                        body: Optional[str] = None) -> dict[str, Any]:
    """Every action ends here: settle, record the trace step, run the login
    detector, and return url/title + snapshot (or `body` text for read)."""
    page = session.page
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=3000)
    except Exception:
        pass
    url = page.url
    try:
        title = await page.title()
    except Exception:
        title = ""
    session.actions += 1
    session.record(action, selector, value, url, title)
    login = await detect_login_page(page)
    if login:
        session.login_seen = True
        session.login_reason = login
        session.svc.keep_page(page)
        log("BROWSER", f"!!! login page detected: {login}")
        return _text(f"{note}\nurl: {url}\nLOGIN_PAGE_DETECTED ({login}). STOP: do not click, fill or press anything "
                     f"on this page. Reply with exactly LOGIN_REQUIRED.")
    if body is not None:
        return _text(f"{note}\nurl: {url}\ntitle: {title}\n--- text ---\n{body}")
    snap = await aria_snapshot(page)
    return _text(f"{note}\nurl: {url}\ntitle: {title}\n--- page snapshot (use these roles/names as selectors) ---\n{snap}")


# ------------------------------------------------------------------ operations
# Each runs ON THE BROWSER LOOP. The MCP tool wrappers marshal to them.
async def op_goto(session: BrowserSession, args: dict) -> dict[str, Any]:
    url = config.normalize_url(str(args.get("url", "")))
    page = await session.ensure_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=config.BROWSER_NAV_TIMEOUT_MS)
    except Exception as e:
        return _err(f"goto failed: {type(e).__name__}: {str(e)[:300]}")
    return await _after_action(session, "goto", args, f"opened {url}", value=url)


class DownloadFailed(Exception):
    """One of four structured reasons, so a client model (or a replay) can act on it
    instead of retrying the same click with a different selector."""

    REASONS = ("no_download_started", "aborted", "timeout", "empty_file")

    def __init__(self, reason: str, detail: str = "") -> None:
        assert reason in self.REASONS, reason
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _unique_dest(name: str) -> Path:
    config.BROWSER_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.BROWSER_DOWNLOAD_DIR / (Path(name or "download").name or "download")
    stem, suffix, n = dest.stem, dest.suffix, 1
    while dest.exists():                          # never silently overwrite an earlier run's file
        dest = config.BROWSER_DOWNLOAD_DIR / f"{stem} ({n}){suffix}"
        n += 1
    return dest


async def save_download(page, click: Callable[[], Awaitable[Any]], timeout_ms: Optional[int] = None) -> Path:
    """Perform `click`, wait for the download it starts, and SAVE it under its own name
    in config.BROWSER_DOWNLOAD_DIR. Returns the absolute path - the path is the whole
    point: a later desktop step attaches the file by it.

    How the bytes move: Playwright already sets CDP Browser.setDownloadBehavior to
    allowAndName with our downloads_path and eventsEnabled at context launch, and
    maps Browser.downloadProgress to the Download object. We do not re-send that
    command (re-sending it on a page session breaks Playwright's own tracking); we
    bound every wait and verify the file instead. Every phase has a deadline:
      start   - expect_download around the click        -> no_download_started
      finish  - dl.path() (completion)                  -> timeout
      failure - dl.failure() (Chrome cancelled it)      -> aborted
      bytes   - size > 0, copy verified                 -> empty_file
    """
    t = (timeout_ms or config.BROWSER_DOWNLOAD_TIMEOUT_MS) / 1000.0
    try:
        async with page.expect_download(timeout=int(t * 1000)) as info:
            await click()
        dl = await info.value
    except Exception as e:
        raise DownloadFailed("no_download_started",
                             f"{type(e).__name__}: the control probably opens a page or a preview instead of "
                             f"downloading - read the page, or find a direct file link. Do not retry this with "
                             f"another selector.") from None
    name = dl.suggested_filename or "download"
    try:
        src = await asyncio.wait_for(dl.path(), timeout=t)
    except asyncio.TimeoutError:
        try:
            await dl.cancel()
        except Exception:
            pass
        raise DownloadFailed("timeout", f"{name}: the transfer did not finish within {t:.0f}s") from None
    except Exception as e:
        # path() raises when the download failed; failure() says why
        try:
            why = await asyncio.wait_for(dl.failure(), timeout=2)
        except Exception:
            why = None
        raise DownloadFailed("aborted", f"{name}: {why or type(e).__name__}") from None
    try:
        why = await asyncio.wait_for(dl.failure(), timeout=2)
    except Exception:
        why = None
    if why:
        raise DownloadFailed("aborted", f"{name}: {why}")
    src = Path(str(src))
    size = src.stat().st_size if src.exists() else 0
    if size <= 0:
        raise DownloadFailed("empty_file", f"{name}: the server sent no bytes")
    dest = _unique_dest(name)
    shutil.copyfile(str(src), str(dest))
    if not dest.exists() or dest.stat().st_size != size:
        raise DownloadFailed("aborted", f"{name}: copy to {dest} did not match ({size} bytes expected)")
    log("BROWSER", f"downloaded {dest.name} ({size} bytes) -> {dest}")
    return dest


async def op_download(session: BrowserSession, args: dict) -> dict[str, Any]:
    """Click something that downloads, and SAVE it where a later [desktop] step can
    reach it. Returns the absolute path."""
    sel = str(args.get("selector", ""))
    page = await session.ensure_page()
    try:
        dest = await save_download(page, lambda: resolve_locator(page, sel).click(timeout=config.BROWSER_ACTION_TIMEOUT_MS))
    except DownloadFailed as e:
        return _err(f"download failed ({e.reason}): {e.detail}")
    return await _after_action(session, "download", args, f"downloaded to {dest}", selector=sel, value=str(dest))


async def op_click(session: BrowserSession, args: dict) -> dict[str, Any]:
    sel = str(args.get("selector", ""))
    page = await session.ensure_page()
    try:
        await resolve_locator(page, sel).click(timeout=config.BROWSER_ACTION_TIMEOUT_MS)
    except Exception as e:
        return _err(f"click {sel!r} failed: {type(e).__name__}: {str(e)[:400]}")
    return await _after_action(session, "click", args, f"clicked {sel}", selector=sel)


async def op_fill(session: BrowserSession, args: dict) -> dict[str, Any]:
    sel = str(args.get("selector", ""))
    text = str(args.get("text", ""))
    if _SECRET_RE.search(text) or _SECRET_RE.search(sel):
        session.login_seen = True
        return _err("Filling credentials or codes is forbidden. Reply LOGIN_REQUIRED.")
    page = await session.ensure_page()
    loc = resolve_locator(page, sel)
    try:
        kind = await loc.get_attribute("type", timeout=config.BROWSER_ACTION_TIMEOUT_MS)
    except Exception as e:
        return _err(f"fill {sel!r} failed: {type(e).__name__}: {str(e)[:400]}")
    if (kind or "").lower() == "password":
        session.login_seen = True
        return _err("That is a password field. Never fill it. Reply LOGIN_REQUIRED.")
    try:
        await loc.fill(text, timeout=config.BROWSER_ACTION_TIMEOUT_MS)
    except Exception as e:
        return _err(f"fill {sel!r} failed: {type(e).__name__}: {str(e)[:400]}")
    return await _after_action(session, "fill", args, f"filled {sel} ({len(text)} chars)", selector=sel, value=text)


async def op_press(session: BrowserSession, args: dict) -> dict[str, Any]:
    key = str(args.get("key", "")).strip()
    sel = str(args.get("selector", "") or "")
    if not key:
        return _err("empty key")
    page = await session.ensure_page()
    try:
        if sel:
            await resolve_locator(page, sel).press(key, timeout=config.BROWSER_ACTION_TIMEOUT_MS)
        else:
            await page.keyboard.press(key)
    except Exception as e:
        return _err(f"press {key!r} failed: {type(e).__name__}: {str(e)[:300]}")
    return await _after_action(session, "press", args, f"pressed {key}", selector=sel or None, value=key)


async def op_select(session: BrowserSession, args: dict) -> dict[str, Any]:
    sel = str(args.get("selector", ""))
    value = str(args.get("value", ""))
    page = await session.ensure_page()
    try:
        await resolve_locator(page, sel).select_option(label=value, timeout=config.BROWSER_ACTION_TIMEOUT_MS)
    except Exception:
        try:
            await resolve_locator(page, sel).select_option(value=value, timeout=config.BROWSER_ACTION_TIMEOUT_MS)
        except Exception as e:
            return _err(f"select {sel!r} failed: {type(e).__name__}: {str(e)[:300]}")
    return await _after_action(session, "select", args, f"selected {value} in {sel}", selector=sel, value=value)


async def op_check(session: BrowserSession, args: dict) -> dict[str, Any]:
    sel = str(args.get("selector", ""))
    page = await session.ensure_page()
    try:
        await resolve_locator(page, sel).check(timeout=config.BROWSER_ACTION_TIMEOUT_MS)
    except Exception as e:
        return _err(f"check {sel!r} failed: {type(e).__name__}: {str(e)[:300]}")
    return await _after_action(session, "check", args, f"checked {sel}", selector=sel)


async def op_wait_for(session: BrowserSession, args: dict) -> dict[str, Any]:
    sel = str(args.get("selector", ""))
    page = await session.ensure_page()
    try:
        await resolve_locator(page, sel).first.wait_for(state="visible", timeout=config.BROWSER_ACTION_TIMEOUT_MS)
    except Exception as e:
        return _err(f"wait_for {sel!r} failed: {type(e).__name__}: {str(e)[:300]}")
    return await _after_action(session, "wait_for", args, f"{sel} is visible", selector=sel)


async def op_read(session: BrowserSession, args: dict) -> dict[str, Any]:
    sel = str(args.get("selector", "") or "")
    page = await session.ensure_page()
    try:
        loc = resolve_locator(page, sel).first if sel else page.locator("body")
        text = await loc.inner_text(timeout=config.BROWSER_ACTION_TIMEOUT_MS)
    except Exception as e:
        return _err(f"read {sel!r} failed: {type(e).__name__}: {str(e)[:300]}")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    cap = config.BROWSER_READ_MAX_CHARS
    if len(text) > cap:
        text = text[:cap] + f"\n... (truncated, {len(text) - cap} more chars)"
    return await _after_action(session, "read", args, f"read {sel or 'the page'}", selector=sel or None, body=text)


async def op_snapshot(session: BrowserSession, args: dict) -> dict[str, Any]:
    page = await session.ensure_page()
    return await _after_action(session, "snapshot", args, "snapshot")


async def op_screenshot(session: BrowserSession, args: dict) -> dict[str, Any]:
    page = await session.ensure_page()
    try:
        raw = await page.screenshot(type="jpeg", quality=config.SCREENSHOT_JPEG_QUALITY)
    except Exception as e:
        return _err(f"screenshot failed: {e!r}")
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
        if w > config.BROWSER_SCREENSHOT_MAX_WIDTH:
            img = img.resize((config.BROWSER_SCREENSHOT_MAX_WIDTH, int(h * config.BROWSER_SCREENSHOT_MAX_WIDTH / w)))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=config.SCREENSHOT_JPEG_QUALITY, optimize=True)
            raw = buf.getvalue()
    except Exception:
        pass
    session.record("screenshot", None, None, page.url, "")
    login = await detect_login_page(page)
    if login:
        session.login_seen = True
        session.svc.keep_page(page)
        return _text(f"LOGIN_PAGE_DETECTED ({login}). STOP and reply with exactly LOGIN_REQUIRED.")
    return {"content": [{"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mimeType": "image/jpeg"},
                        {"type": "text", "text": f"screenshot of {page.url} (for looking only; act through selectors from snapshot)"}]}


async def op_scroll(session: BrowserSession, args: dict) -> dict[str, Any]:
    direction = str(args.get("direction", "down")).lower()
    amount = int(args.get("amount", 600) or 600)
    page = await session.ensure_page()
    dy = -amount if direction == "up" else amount
    try:
        await page.mouse.wheel(0, dy)
    except Exception as e:
        return _err(f"scroll failed: {e!r}")
    return await _after_action(session, "scroll", args, f"scrolled {direction}", value=direction)


async def op_back(session: BrowserSession, args: dict) -> dict[str, Any]:
    page = await session.ensure_page()
    try:
        await page.go_back(wait_until="domcontentloaded")
    except Exception as e:
        return _err(f"back failed: {e!r}")
    return await _after_action(session, "back", args, "went back")


OPS: dict[str, Callable[[BrowserSession, dict], Awaitable[dict]]] = {
    "goto": op_goto, "click": op_click, "fill": op_fill, "press": op_press, "select": op_select,
    "download": op_download,
    "check": op_check, "wait_for": op_wait_for, "read": op_read, "snapshot": op_snapshot,
    "screenshot": op_screenshot, "scroll": op_scroll, "back": op_back,
}


# ---------------------------------------------------------------------------
# Verifier (`recipe-engine check`). NEVER touches the real profile.
# ---------------------------------------------------------------------------
def verify() -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="voice-agent-pw-"))
    svc = BrowserService().start()

    async def _go():
        ctx = await svc.ensure_context(profile_dir=tmp, headless=True)
        page = await ctx.new_page()
        await page.set_content("<button>Save</button><a href='#g'>Invoices</a>"
                               "<label>Search <input placeholder='Find'></label>")
        assert await resolve_locator(page, "role=button[name='Save']").count() == 1
        assert await resolve_locator(page, "text=Invoices").count() == 1
        assert await resolve_locator(page, "label=Search").count() == 1
        assert await resolve_locator(page, "placeholder=Find").count() == 1
        assert await detect_login_page(page) is None
        snap = await aria_snapshot(page)
        assert "Invoices" in snap and "Save" in snap, snap
        await page.set_content("<form><input type='text'><input type='password'></form>")
        assert await detect_login_page(page) == "visible password field"
        await page.goto("about:blank")
        version = ctx.browser.version if ctx.browser else "?"
        await svc._close()
        return {"chrome": version, "snapshot_chars": len(snap)}

    try:
        out = svc.run(_go(), timeout=config.BROWSER_LAUNCH_TIMEOUT_S + 30)
    finally:
        svc.close()
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
    assert _SECRET_RE.search("enter your password") and not _SECRET_RE.search("invoices table")
    out["semantic_only"] = is_semantic_selector("role=link[name='x']") and not is_semantic_selector("#id")
    return out


if __name__ == "__main__":
    if "--verify" in sys.argv:
        print(json.dumps(verify(), default=str))
        sys.exit(0)
    if "--pids" in sys.argv:
        print("chrome on the agent profile:", profile_chrome_pids())
        sys.exit(0)
    # `python browser.py <url>`: open the url on the REAL profile (takes it over) and wait until closed.
    url = next((a for a in sys.argv[1:] if not a.startswith("--")), "about:blank")
    print("opened:", open_url(url))
    print("close the browser window to exit ...")
    wait_until_closed()
