"""Source-level pins on browser.py that need no Chrome: the credential gate in op_fill,
the login detector after every op, and the download tool's structured failure reasons.
The invariant self-test covers the same ground by mutation; these are the readable form."""
from __future__ import annotations

import inspect
import re

from recipe_engine import browser, config, safety


def test_op_fill_has_all_three_credential_layers():
    src = inspect.getsource(browser.op_fill)
    assert "_SECRET_RE.search(text)" in src and "_SECRET_RE.search(sel)" in src, "text AND selector"
    assert '"password"' in src, "resolved element type"
    assert src.count("session.login_seen = True") >= 2, "both refusals mark the run un-cacheable"


def test_every_op_in_the_table_ends_in_after_action_including_download():
    for name, fn in browser.OPS.items():
        src = inspect.getsource(fn)
        if name == "screenshot":
            assert "detect_login_page(" in src, name
        else:
            assert "_after_action(" in src, f"{name} skips login detection"


def test_the_one_secret_regex_is_shared():
    assert browser._SECRET_RE is safety._SECRET_RE


def test_download_has_four_structured_reasons_and_every_wait_is_bounded():
    """A 'Download' control that opens a preview must read differently from a transfer
    that started and died, stalled, or produced nothing - or the client retries the
    same click with new selectors. The live outcomes are in tests/test_download.py;
    this pins the contract in the source."""
    assert browser.DownloadFailed.REASONS == ("no_download_started", "aborted", "timeout", "empty_file")
    src = inspect.getsource(browser.save_download)
    for reason in browser.DownloadFailed.REASONS:
        assert f'DownloadFailed("{reason}"' in src, reason
    assert "Do not retry this with " in src and "another selector." in src   # the sentence spans two f-string fragments
    assert "expect_download(timeout=" in src                       # start is bounded
    assert "asyncio.wait_for(dl.path(), timeout=t)" in src          # completion is bounded
    assert "if size <= 0:" in src                                   # a 0-byte file is a failure
    assert "time.sleep" not in src and "asyncio.sleep" not in src
    op = inspect.getsource(browser.op_download)
    assert "save_download(" in op and "except DownloadFailed" in op and "_after_action(" in op


def test_downloads_are_staged_in_the_engine_data_dir():
    whole = inspect.getsource(browser)
    assert "downloads_path=str(config.BROWSER_DOWNLOAD_DIR)" in whole
    assert "accept_downloads=True" in whole
    assert config.BROWSER_DOWNLOAD_DIR.parent == config.DATA_DIR


def test_send_is_destructive_so_it_is_never_replayed():
    assert "send" in config.DESTRUCTIVE_WORDS
    assert safety.is_destructive("type the questions into the project and send it")
    assert re.search(r"\bsubmit\b", "|".join(config.DESTRUCTIVE_WORDS))
