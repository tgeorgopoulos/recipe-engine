"""The per-install token and the middleware that enforces it.

Anything that can talk to the engine's port can drive the user's desktop, so the
port is bound to loopback AND every request must carry the token. The token is a
random 256-bit string written once to %LOCALAPPDATA%\\RecipeEngine\\token with a
DACL that only the current Windows user can read; clients (the Node shim in the
.mcpb, the Cowork plugin's .mcp.json, the panel) read it from there.

The middleware is plain ASGI on purpose - no OAuth metadata, no issuer, no
discovery - because the client and the server are the same user on the same
machine. `hmac.compare_digest` keeps the comparison constant-time.
"""
from __future__ import annotations

import hmac
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs

from recipe_engine import config
from recipe_engine.config import log


def token_path() -> Path:
    return Path(config.TOKEN_FILE)


def _restrict_acl(p: Path) -> None:
    """Only the current user may read the token file. pywin32 first (exact DACL),
    icacls as the fallback; on non-Windows chmod 0600."""
    if sys.platform != "win32":
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
        return
    try:
        import win32api
        import win32con
        import win32security
        user, domain, _ = win32security.LookupAccountName(None, win32api.GetUserName())
        sd = win32security.GetFileSecurity(str(p), win32security.DACL_SECURITY_INFORMATION)
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, user)
        sd.SetSecurityDescriptorDacl(1, dacl, 0)
        win32security.SetFileSecurity(str(p), win32security.DACL_SECURITY_INFORMATION, sd)
        return
    except Exception as e:
        log("AUTH", f"pywin32 ACL failed ({e!r}); falling back to icacls")
    try:
        me = os.environ.get("USERNAME") or os.getlogin()
        subprocess.run(["icacls", str(p), "/inheritance:r", "/grant:r", f"{me}:(R,W)"],
                       capture_output=True, timeout=20)
    except Exception as e:
        log("AUTH", f"icacls failed ({e!r}); token file keeps inherited permissions")


def _write(p: Path, token: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(token, encoding="ascii")
    os.replace(tmp, p)
    _restrict_acl(p)


def generate() -> str:
    return secrets.token_urlsafe(32)


def ensure_token() -> str:
    """Return the install token, creating it on first run."""
    p = token_path()
    if p.exists():
        t = p.read_text(encoding="ascii").strip()
        if t:
            return t
    t = generate()
    _write(p, t)
    log("AUTH", f"new install token written to {p}")
    return t


def read_token() -> str:
    p = token_path()
    try:
        return p.read_text(encoding="ascii").strip()
    except OSError:
        return ""


def rotate_token() -> str:
    t = generate()
    _write(token_path(), t)
    log("AUTH", "install token rotated; clients must re-read it")
    return t


class _TokenCache:
    """Re-reads the token when the file changes, so `recipe-engine token --rotate`
    takes effect without restarting the service."""

    def __init__(self) -> None:
        self._value = ""
        self._mtime = -1.0
        self._checked = 0.0

    def __call__(self) -> str:
        now = time.time()
        if now - self._checked > 1.0:
            self._checked = now
            try:
                m = token_path().stat().st_mtime
            except OSError:
                m = -1.0
            if m != self._mtime:
                self._mtime = m
                self._value = read_token()
        return self._value


current_token: Callable[[], str] = _TokenCache()


class BearerAuthMiddleware:
    """Pure ASGI: 401 unless `Authorization: Bearer <token>` matches. `exempt` paths
    pass without a token: the health probe and the panel's static page (plain HTML with
    no secrets in it - a browser cannot send a bearer header for a page, so the tray's
    `/#t=<token>` link needs the page first; every /api route and /mcp stay gated). The
    SSE event stream may carry the token as `?t=` because EventSource cannot set headers."""

    PAGE_PATHS = ("/", "/index.html", "/favicon.ico")

    def __init__(self, app, token_provider: Callable[[], str] = current_token,
                 exempt: tuple[str, ...] = ("/api/health",) + PAGE_PATHS, query_token_paths: tuple[str, ...] = ("/api/events",)):
        self.app = app
        self.token_provider = token_provider
        self.exempt = exempt
        self.query_token_paths = query_token_paths

    def _given(self, scope) -> Optional[str]:
        for k, v in scope.get("headers") or []:
            if k == b"authorization":
                val = v.decode("latin-1", "replace")
                if val.lower().startswith("bearer "):
                    return val[7:].strip()
                return val.strip()
        if scope.get("path") in self.query_token_paths:
            qs = parse_qs((scope.get("query_string") or b"").decode("latin-1", "replace"))
            if qs.get("t"):
                return qs["t"][0]
        return None

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path") or ""
        if path in self.exempt:
            return await self.app(scope, receive, send)
        expected = self.token_provider() or ""
        given = self._given(scope) or ""
        if expected and given and hmac.compare_digest(given.encode(), expected.encode()):
            return await self.app(scope, receive, send)
        body = b'{"error":"unauthorized","hint":"send Authorization: Bearer <token from %LOCALAPPDATA%\\\\RecipeEngine\\\\token>"}'
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"www-authenticate", b"Bearer"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})
