"""The panel routes on the real ASGI app (MCP app + panel + token middleware)."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from recipe_engine import auth, config, service
from recipe_engine.engine import Engine


@pytest.fixture(scope="module")
def client():
    token = auth.ensure_token()
    app = service.build_app(Engine())
    with TestClient(app, base_url=f"http://127.0.0.1:{config.PORT}") as c:
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


def test_health_and_the_static_page_need_no_token_but_every_api_route_does(client):
    bare = TestClient(service.build_app(Engine()), base_url=f"http://127.0.0.1:{config.PORT}")
    assert bare.get("/api/health").status_code == 200
    assert bare.get("/api/state").status_code == 401
    assert bare.get("/api/recipes").status_code == 401
    assert bare.post("/api/recipes/x/replay", json={}).status_code == 401
    # the page is plain HTML the browser must load BEFORE it can read the token from the
    # tray's /#t= link; it carries no secret, and every API call it makes is gated above
    page = bare.get("/")
    assert page.status_code == 200 and b"RECIPE ENGINE" in page.content
    assert auth.ensure_token().encode() not in page.content


def test_state_and_page(client):
    s = client.get("/api/state").json()
    assert s["invariants_ok"] is True and "input_owned" in s and s["recipes"] >= 0
    page = client.get("/")
    assert page.status_code == 200 and b"RECIPE ENGINE" in page.content


def test_foreign_host_is_refused_and_posts_must_be_json(client):
    r = client.get("/api/state", headers={"Host": "evil.example:80"})
    assert r.status_code == 403
    r = client.post("/api/recipes/forget", content="name=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400


def test_replay_of_unknown_recipe_is_a_structured_failure(client):
    r = client.post("/api/recipes/does-not-exist/replay", json={"inputs": {}})
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "failed" and j["failure_reason"].startswith("no_such_recipe")


def test_abort_with_nothing_running_is_false(client):
    assert client.post("/api/replay/abort", json={}).json() == {"ok": False}


def test_mcp_endpoint_is_behind_the_same_token(client):
    bare = TestClient(service.build_app(Engine()), base_url=f"http://127.0.0.1:{config.PORT}")
    assert bare.post(config.MCP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
