"""
Saved browser login (V4.1): the live cookie jar, snapshotted and restored,
plus a record of which sites were SEEN signed in.

Why this exists
---------------
some portals (`portal_session`), Microsoft SSO (`ESTSAUTH`) and most SSO providers
keep you signed in with SESSION cookies - no expiry, held in memory. Chrome
drops those on its next launch unless the profile is set to "continue where
you left off", so the agent's fresh persistent context started every session
logged out, even though the user had just signed in by hand. The profile's
Cookies DB is also written in 30 s batches, so a Chrome that was taken over
(killed) may never have flushed the login at all.

So the BrowserService snapshots `context.storage_state()` (which includes
session cookies) every SESSION_SNAPSHOT_S seconds and on every page load while
the browser is up, and `ensure_context` adds those cookies back right after
launch. The snapshot is the user's live sessions, so on Windows it is sealed
with DPAPI (CryptProtectData: only this Windows user on this machine can open
it); elsewhere it is a 0600 file.

"Has cookies" is not "is signed in": some portals give every anonymous visitor a
`portal_session`. So the store also keeps `verified` - hosts where a page was
actually observed signed in (a Log out / Sign out control on a non-login page,
or a login-looking URL that redirected to a real page by itself). Those are
the hosts the planner is told to use, and the ones the spoken login message
calls a saved login.

Nothing here ever holds a password. Cookies only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from recipe_engine import config
from recipe_engine.config import log

_MAGIC_DPAPI = b"VADPAPI1"
_MAGIC_PLAIN = b"VAPLAIN1"
VERIFIED_TTL_S = 30 * 86400          # a sign-in observed longer ago than this no longer counts


# ------------------------------------------------------------------ sealing
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    import ctypes.wintypes as W

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", W.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    desc = ctypes.c_wchar_p("voice-agent browser session") if protect else None
    # CRYPTPROTECT_UI_FORBIDDEN = 1: never pop a dialog
    if not fn(ctypes.byref(inp), desc, None, None, None, 1, ctypes.byref(out)):
        raise OSError(f"DPAPI {'protect' if protect else 'unprotect'} failed: {ctypes.GetLastError()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def seal(data: bytes) -> bytes:
    if sys.platform == "win32":
        return _MAGIC_DPAPI + _dpapi(data, True)
    return _MAGIC_PLAIN + data


def unseal(blob: bytes) -> bytes:
    if blob.startswith(_MAGIC_DPAPI):
        if sys.platform != "win32":
            raise OSError("snapshot was sealed with Windows DPAPI")
        return _dpapi(blob[len(_MAGIC_DPAPI):], False)
    if blob.startswith(_MAGIC_PLAIN):
        return blob[len(_MAGIC_PLAIN):]
    raise OSError("not a voice-agent session snapshot")


# ------------------------------------------------------------------ store
def path() -> Path:
    return Path(config.SESSION_STATE_FILE)


def _write(payload: dict[str, Any]) -> Optional[Path]:
    try:
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(seal(json.dumps(payload).encode("utf-8")))
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        return p
    except Exception as e:
        log("SESSION", f"could not write the browser session snapshot: {e!r}")
        return None


def load() -> Optional[dict[str, Any]]:
    """The last snapshot, or None. Never raises."""
    p = path()
    if not p.exists():
        return None
    try:
        return json.loads(unseal(p.read_bytes()).decode("utf-8"))
    except Exception as e:
        log("SESSION", f"could not read the browser session snapshot ({e!r}); ignoring it")
        return None


def save(state: dict[str, Any]) -> Optional[Path]:
    """Persist a Playwright storage_state dict (cookies + origins), keeping the
    `verified` record of the previous snapshot. Never raises."""
    cookies = [c for c in state.get("cookies", []) if c.get("name") and c.get("value")]
    prev = load() or {}
    return _write({"saved_at": time.time(), "cookies": cookies, "origins": state.get("origins", []),
                   "verified": prev.get("verified", {})})


def mark_verified(host_or_url: str) -> None:
    """A page on this host was observed signed in. Never raises."""
    host = _host_of(host_or_url)
    if not host:
        return
    snap = load() or {"saved_at": 0, "cookies": [], "origins": []}
    ver = snap.setdefault("verified", {})
    first = host not in ver
    ver[host] = time.time()
    _write(snap)
    if first:
        log("SESSION", f"confirmed sign-in at {host}")


def forget() -> bool:
    p = path()
    if p.exists():
        p.unlink()
        return True
    return False


# ------------------------------------------------------------------ queries
def _host_of(url_or_host: str) -> str:
    s = str(url_or_host or "")
    h = urlsplit(s).hostname if "://" in s else s.split("/")[0]
    return (h or "").lower().lstrip(".")


def _cookie_matches(cookie_domain: str, host: str) -> bool:
    d = (cookie_domain or "").lower().lstrip(".")
    return bool(d) and (host == d or host.endswith("." + d))


def cookies_for(host_or_url: str, snapshot: Optional[dict] = None) -> list[dict]:
    snap = snapshot if snapshot is not None else load()
    if not snap:
        return []
    host = _host_of(host_or_url)
    return [c for c in snap.get("cookies", []) if _cookie_matches(c.get("domain", ""), host)]


# Names that mean "some kind of session", per site family. Anything else (analytics,
# CSRF, preferences) does not count. NOTE: present != signed in (see module docstring).
SESSION_COOKIE_HINTS = ("session", "sess", "sid", "auth", "token", "login", "estsauth", "ssid", "jsessionid",
                        "_normandy", "pseudonym", "remember", "persistent", "shib", "saml", "okta", "duo")


def verified_hosts(snapshot: Optional[dict] = None) -> list[str]:
    """Hosts observed signed in within VERIFIED_TTL_S, most recent first."""
    snap = snapshot if snapshot is not None else load()
    if not snap:
        return []
    now = time.time()
    ver = {h: float(t) for h, t in (snap.get("verified") or {}).items() if now - float(t) < VERIFIED_TTL_S}
    return sorted(ver, key=lambda h: -ver[h])


def _age_words(t: float) -> str:
    age = max(0.0, time.time() - t)
    if age < 90:
        return "just now"
    if age < 5400:
        return f"{int(age // 60)} minutes ago"
    if age < 172800:
        return f"{age / 3600:.0f} hours ago"
    return f"{age / 86400:.0f} days ago"


def describe(host_or_url: str, snapshot: Optional[dict] = None) -> tuple[bool, str]:
    """(confirmed_sign_in, plain-words sentence) for a site."""
    snap = snapshot if snapshot is not None else load()
    host = _host_of(host_or_url)
    if not snap:
        return False, f"I have no saved login for {host}."
    ver = snap.get("verified") or {}
    if host in ver and time.time() - float(ver[host]) < VERIFIED_TTL_S:
        return True, f"I saw you signed in at {host} {_age_words(float(ver[host]))}."
    mine = cookies_for(host, snap)
    sessiony = [c for c in mine if any(h in c.get("name", "").lower() for h in SESSION_COOKIE_HINTS)]
    if sessiony:
        return False, f"I have cookies for {host} from {_age_words(float(snap.get('saved_at', 0) or 0))}, but I never saw you signed in there."
    return False, f"I have no saved login for {host}."


def hosts_with_logins(snapshot: Optional[dict] = None) -> list[str]:
    """Confirmed sign-ins (for the planner, the panel and the --login-browser summary)."""
    return verified_hosts(snapshot)


_SKIP_TOKENS = {"www", "com", "edu", "org", "net", "io", "gov", "login", "auth", "sso", "accounts", "account",
                "signin", "sign", "in", "portal", "app", "my", "web", "online"}


def alternative_host(host_or_url: str, snapshot: Optional[dict] = None) -> Optional[str]:
    """A CONFIRMED host for the same service as a host that just asked for a login:
    acme.vendorportal.com -> portal.acme.com (they share 'acme'). None if there is none."""
    host = _host_of(host_or_url)
    tokens = {t for t in re.split(r"[.\-]", host) if len(t) >= 3 and t not in _SKIP_TOKENS}
    for cand in verified_hosts(snapshot):
        if cand == host:
            continue
        ctoks = {t for t in re.split(r"[.\-]", cand) if len(t) >= 3 and t not in _SKIP_TOKENS}
        if tokens & ctoks:
            return cand
    return None


if __name__ == "__main__":
    import tempfile
    fake = {"cookies": [
        {"name": "portal_session", "value": "x", "domain": "acme.vendorportal.com", "path": "/", "expires": -1},
        {"name": "_ga", "value": "y", "domain": ".acme.com", "path": "/", "expires": 1e10},
        {"name": "ESTSAUTHPERSISTENT", "value": "z", "domain": ".login.microsoftonline.com", "path": "/", "expires": 1e10},
    ], "origins": []}
    old = config.SESSION_STATE_FILE
    config.SESSION_STATE_FILE = Path(tempfile.mkdtemp()) / "s.bin"
    try:
        assert save(fake) is not None and path().exists()
        assert not path().read_bytes().startswith(b"{"), "snapshot must not be plaintext JSON"
        got = load()
        assert [c["name"] for c in got["cookies"]] == ["portal_session", "_ga", "ESTSAUTHPERSISTENT"]
        ok, words = describe("https://acme.vendorportal.com/invoices")
        assert ok is False and "never saw you signed in" in words, words       # a cookie is not a login
        assert describe("https://example.com")[1].startswith("I have no saved login")
        assert hosts_with_logins() == [] and alternative_host("acme.vendorportal.com") is None
        mark_verified("https://portal.acme.com/invoices?x=1")
        assert describe("portal.acme.com")[0] is True and "signed in at portal.acme.com just now" in describe("portal.acme.com")[1]
        assert hosts_with_logins() == ["portal.acme.com"]
        assert alternative_host("https://acme.vendorportal.com/login/sso") == "portal.acme.com"     # shares "acme"
        assert alternative_host("https://login.portal.acme.com/") == "portal.acme.com"
        assert alternative_host("https://mail.google.com") is None
        save(fake)                                                              # a cookie refresh keeps `verified`
        assert hosts_with_logins() == ["portal.acme.com"]
        assert forget() and load() is None
        print("session_store self-test ok")
    finally:
        config.SESSION_STATE_FILE = old
