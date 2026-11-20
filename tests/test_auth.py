"""The install token and the bearer middleware. The token lives in the test data dir
(conftest points RECIPE_ENGINE_DATA_DIR at a temp directory)."""
from __future__ import annotations

import sys

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from recipe_engine import auth


def _app():
    async def ok(request):
        return JSONResponse({"ok": True})
    inner = Starlette(routes=[Route("/api/health", ok), Route("/api/state", ok), Route("/api/events", ok), Route("/mcp", ok, methods=["GET", "POST"])])
    return auth.BearerAuthMiddleware(inner, lambda: "s3cret")


def test_token_is_created_once_and_is_random():
    t1 = auth.ensure_token()
    assert len(t1) >= 40 and auth.token_path().exists()
    assert auth.ensure_token() == t1, "a second call must not rotate the token"
    t2 = auth.rotate_token()
    assert t2 != t1 and auth.read_token() == t2


@pytest.mark.skipif(sys.platform != "win32", reason="ACL check is Windows-specific")
def test_token_file_is_readable_only_by_the_current_user():
    import win32api
    import win32security
    auth.ensure_token()
    sd = win32security.GetFileSecurity(str(auth.token_path()), win32security.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    assert dacl is not None and dacl.GetAceCount() >= 1
    me, _, _ = win32security.LookupAccountName(None, win32api.GetUserName())
    sids = [dacl.GetAce(i)[2] for i in range(dacl.GetAceCount())]      # PySID is not hashable
    assert len(sids) == 1 and sids[0] == me, f"token ACL grants {len(sids)} principals, expected only the current user"


def test_middleware_401s_without_the_token_and_passes_with_it():
    c = TestClient(_app())
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/state", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = c.get("/api/state", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert c.post("/mcp", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_health_is_exempt_and_events_may_use_the_query_token():
    c = TestClient(_app())
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/events?t=s3cret").status_code == 200
    assert c.get("/api/events?t=nope").status_code == 401
    assert c.get("/api/state?t=s3cret").status_code == 401, "query tokens are only for the SSE route"


def test_rotation_is_picked_up_without_restart():
    cache = auth._TokenCache()
    t1 = auth.ensure_token()
    assert cache() == t1
    cache._checked = 0.0                       # let the cache re-stat immediately
    t2 = auth.rotate_token()
    assert cache() == t2
