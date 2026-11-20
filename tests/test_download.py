"""Downloads are bounded and verified. The old tool could sit forever on a stalled
transfer and report success on a 0-byte file; `save_download` gives one of four
structured reasons instead, and the replay path returns the saved file's path.

Fixture routes (tests/serve_fixture.py, variant A):
  /report.csv     real attachment          -> ok, file saved under its own name
  /empty.csv      0-byte attachment         -> empty_file
  /slow-download  stalls after 1 KB (40 s)  -> timeout, in ~the budget, not 40 s
  /invoices.html    ordinary page             -> no_download_started
Runs on a TEMP profile + TEMP download dir; the real ones are never touched.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from recipe_engine import config

pytestmark = pytest.mark.slow           # real Chrome (~10 s for the module)

FIXTURE_PORT = 8766


@pytest.fixture(scope="module")
def site():
    from tests.serve_fixture import serve_in_thread
    srv = serve_in_thread("A", port=FIXTURE_PORT)
    yield f"http://127.0.0.1:{FIXTURE_PORT}"
    srv.shutdown()


@pytest.fixture
def temp_browser(monkeypatch, tmp_path):
    from recipe_engine import browser
    monkeypatch.setattr(config, "BROWSER_PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(config, "BROWSER_DOWNLOAD_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config, "SESSION_STATE_FILE", tmp_path / "session.bin")
    monkeypatch.setattr(config, "BROWSER_HEADLESS", True)
    monkeypatch.setattr(config, "BROWSER_TAKE_OVER_PROFILE", False)
    monkeypatch.setattr(config, "BROWSER_DOWNLOAD_TIMEOUT_MS", 4_000)   # the point is the bound, not 15 s of waiting
    browser.close_service()
    yield tmp_path / "downloads"
    browser.close_service()


def _run(coro, timeout=60):
    from recipe_engine import browser
    return browser.service().run(coro, timeout=timeout)


def _session():
    from recipe_engine import browser
    return browser.BrowserSession(svc=browser.service())


def _download_via_tool(site: str, link_text: str) -> dict:
    """The MCP `browser_download` path: op_download on a session that is on /downloads.html."""
    from recipe_engine import browser

    async def go():
        s = _session()
        page = await s.ensure_page()
        await page.goto(site + "/downloads.html", wait_until="domcontentloaded")
        out = await browser.op_download(s, {"selector": f"text={link_text}"})
        return out, s.trace
    return _run(go())


def _text_of(out: dict) -> str:
    return "".join(c.get("text", "") for c in out.get("content", []))


def test_a_real_attachment_is_saved_under_its_own_name(site, temp_browser):
    out, trace = _download_via_tool(site, "Download report")
    txt = _text_of(out)
    assert not out.get("is_error"), txt
    saved = temp_browser / "report.csv"
    assert saved.exists() and saved.stat().st_size > 1000, txt
    assert str(saved) in txt, txt                                   # the absolute path is the deliverable
    assert trace and trace[-1]["action"] == "download" and trace[-1]["value"] == str(saved)


def test_a_second_download_of_the_same_name_does_not_overwrite(site, temp_browser):
    _download_via_tool(site, "Download report")
    out, _ = _download_via_tool(site, "Download report")
    assert (temp_browser / "report.csv").exists()
    assert (temp_browser / "report (1).csv").exists(), sorted(p.name for p in temp_browser.iterdir())
    assert "report (1).csv" in _text_of(out)


def test_an_empty_file_is_a_failure_not_a_success(site, temp_browser):
    out, _ = _download_via_tool(site, "Download empty file")
    txt = _text_of(out)
    assert out.get("is_error") and "empty_file" in txt, txt
    assert not (temp_browser / "empty.csv").exists(), "a 0-byte file must not be left where a later step could attach it"


def test_a_link_that_opens_a_page_reports_no_download_started(site, temp_browser):
    t0 = time.time()
    out, _ = _download_via_tool(site, "Open invoices page")
    txt = _text_of(out)
    assert out.get("is_error") and "no_download_started" in txt, txt
    assert "Do not retry" in txt                                    # the reason tells the client not to thrash
    assert time.time() - t0 < config.BROWSER_DOWNLOAD_TIMEOUT_MS / 1000 + 6


def test_a_stalled_transfer_times_out_within_the_budget(site, temp_browser):
    t0 = time.time()
    out, _ = _download_via_tool(site, "Download slow file")
    took = time.time() - t0
    txt = _text_of(out)
    assert out.get("is_error") and "timeout" in txt, txt
    assert took < config.BROWSER_DOWNLOAD_TIMEOUT_MS / 1000 + 6, f"waited {took:.1f}s for a stalled download"
    assert not list(temp_browser.glob("slow*")), "a partial file must not be reported or left behind"


def test_replay_of_a_download_step_returns_the_fresh_file_path(site, temp_browser):
    """A recipe whose browser steps are goto -> download: the replay's read_texts carry the
    path of the file THIS run saved, so the desktop half can attach it."""
    from recipe_engine import recipes

    r = recipes.Recipe(id="dl-test", command_normalized="download the report", surface="browser", steps=[
        {"surface": "browser", "action": "goto", "value": site + "/downloads.html", "expect": "text=Download report"},
        {"surface": "browser", "action": "download", "selector": "text=Download report", "expect": "url=" + site + "/downloads.html"},
    ])

    async def go():
        page = await _session().ensure_page()
        return await recipes.replay_browser_steps(page, r)
    texts = _run(go())
    assert texts and texts[-1].startswith("downloaded file saved at: "), texts
    p = Path(texts[-1].split(": ", 1)[1])
    assert p.exists() and p.parent == temp_browser and p.stat().st_size > 1000


def test_replay_of_a_failed_download_names_the_reason(site, temp_browser):
    from recipe_engine import recipes

    r = recipes.Recipe(id="dl-test-2", command_normalized="download the empty report", surface="browser", steps=[
        {"surface": "browser", "action": "goto", "value": site + "/downloads.html", "expect": "text=Download empty file"},
        {"surface": "browser", "action": "download", "selector": "text=Download empty file", "expect": "url=" + site + "/downloads.html"},
    ])

    async def go():
        page = await _session().ensure_page()
        return await recipes.replay_browser_steps(page, r)
    with pytest.raises(recipes.ReplayFailed) as ei:
        _run(go())
    assert ei.value.step == 2 and "empty_file" in ei.value.reason, (ei.value.step, ei.value.reason)
