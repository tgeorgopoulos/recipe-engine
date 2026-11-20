"""config.normalize_url is the ONE place a URL is cleaned up before a browser
sees it. Three entry points share it -- browser.open_url (used by
--login-browser and by recipe replay), the browser `goto` tool and the desktop
`open_browser` tool -- so a regression here breaks all of them at once.

The regression that motivated the helper: every call site tested
`re.match(r"^[a-z]+://", url)` and prepended "https://" when it failed, which
turned the default "about:blank" into "https://about:blank". Playwright then
raised on a navigation that could never succeed, so `python main.py
--login-browser` died with a traceback before the user could sign in.
"""
import pytest

from recipe_engine import config
from recipe_engine.config import normalize_url

# The cases the fix was specified against, verbatim. Four of these nine fail on
# the pre-fix code (about:blank, data:, mailto: all get "https://" glued on;
# localhost picks the wrong scheme).
#
# DOCUMENTED DEVIATION -- "localhost:8000":
#   This is the ambiguous case. "localhost:8000" parses as scheme "localhost"
#   followed by "8000", so a scheme regex alone would leave it untouched and
#   Playwright would reject it. The bare-host check below handles it: a colon
#   followed only by digits is a PORT, not a scheme, so the string is treated as
#   a host and given a scheme.
#   Which scheme: loopback gets http, everything else https. The original spec
#   said https here; http is used instead because nothing in this project serves
#   TLS on localhost (tests/serve_fixture.py is a plain http.server on
#   127.0.0.1:8765, and the saved recipes replay http://127.0.0.1:8765), and it
#   matches what Chrome's omnibox does with a typed "localhost:8000".
SPEC_CASES = [
    # already carry a scheme -> unchanged
    ("about:blank", "about:blank"),
    ("chrome://version", "chrome://version"),
    ("file:///C:/x.html", "file:///C:/x.html"),
    ("data:text/html,<p>x", "data:text/html,<p>x"),
    ("mailto:a@b.com", "mailto:a@b.com"),
    ("https://portal.acme.com", "https://portal.acme.com"),
    ("http://localhost:8000", "http://localhost:8000"),
    # bare hosts -> a scheme is added
    ("portal.acme.com", "https://portal.acme.com"),
    ("localhost:8000", "http://localhost:8000"),   # see DOCUMENTED DEVIATION above
]


@pytest.mark.parametrize("raw, want", SPEC_CASES)
def test_spec_cases(raw, want):
    assert normalize_url(raw) == want


@pytest.mark.parametrize("raw", ["", "   ", "\t\n", None])
def test_empty_input_is_the_blank_page(raw):
    assert normalize_url(raw) == config.BLANK_URL == "about:blank"


@pytest.mark.parametrize("raw", ["about:blank", "about:newtab", "chrome://version",
                                 "data:text/html,<p>hi", "mailto:someone@example.com",
                                 "file:///C:/tmp/page.html", "blob:https://x.com/abc"])
def test_non_http_schemes_are_left_alone(raw):
    """The old code prepended https:// to every one of these."""
    assert normalize_url(raw) == raw


def test_blank_page_never_becomes_an_https_url():
    """The exact --login-browser regression."""
    assert normalize_url("about:blank") != "https://about:blank"
    assert not normalize_url("").startswith("https://")


@pytest.mark.parametrize("raw, want", [
    ("https://vendorportal.com", "https://vendorportal.com"),
    ("http://127.0.0.1:8765/invoices.html", "http://127.0.0.1:8765/invoices.html"),
    ("https://x.com/a/b?q=1&r=2#frag", "https://x.com/a/b?q=1&r=2#frag"),
])
def test_a_full_url_is_unchanged(raw, want):
    assert normalize_url(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("HTTP://Example.com/Path", "http://Example.com/Path"),
    ("HTTPS://Example.com", "https://Example.com"),
    ("About:Blank", "about:Blank"),
])
def test_the_scheme_is_lowercased_but_the_path_is_not(raw, want):
    """Schemes are case-insensitive; paths are not, so only the scheme moves."""
    assert normalize_url(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("vendorportal.com", "https://vendorportal.com"),
    ("gmail.com/mail/u/0", "https://gmail.com/mail/u/0"),
    ("example.com:8443/app", "https://example.com:8443/app"),
    ("//cdn.example.com/x.js", "https://cdn.example.com/x.js"),
])
def test_a_bare_host_gets_https(raw, want):
    assert normalize_url(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("localhost:8765", "http://localhost:8765"),
    ("localhost:8765/invoices.html", "http://localhost:8765/invoices.html"),
    ("127.0.0.1:8765/", "http://127.0.0.1:8765/"),
    ("localhost", "http://localhost"),
    ("[::1]:8080/x", "http://[::1]:8080/x"),
])
def test_loopback_gets_http_not_https(raw, want):
    """Nothing serves TLS on the fixture server tests/serve_fixture.py starts,
    and "localhost:8765" must not be mistaken for a `localhost:` scheme."""
    assert normalize_url(raw) == want


@pytest.mark.parametrize("raw, want", [
    (r"C:\Users\me\page.html", "file:///C:/Users/me/page.html"),
    ("C:/Users/me/page.html", "file:///C:/Users/me/page.html"),
    (r"C:\Users\me\my page.html", "file:///C:/Users/me/my%20page.html"),
    (r"\\server\share\page.html", "file://server/share/page.html"),
])
def test_a_windows_path_becomes_a_file_url(raw, want):
    """`C:\\...` looks like the scheme `c:` -- it must not become https://C:\\..."""
    assert normalize_url(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("  https://x.com  ", "https://x.com"),
    ("<https://x.com>", "https://x.com"),
    ('"https://x.com"', "https://x.com"),
    ("\thttps://x.com\n", "https://x.com"),
])
def test_surrounding_junk_is_stripped(raw, want):
    """A URL pasted from chat or dictated into a recipe arrives wrapped."""
    assert normalize_url(raw) == want


@pytest.mark.parametrize("raw", ["", "about:blank", "vendorportal.com", "localhost:8765",
                                 "https://x.com/a?b=1", r"C:\tmp\p.html", "//cdn.x.com/a.js"])
def test_normalizing_twice_changes_nothing(raw):
    """Call sites chain (the desktop tool normalises, then hands the result to
    browser.open_url, which normalises again), so this must be idempotent."""
    once = normalize_url(raw)
    assert normalize_url(once) == once


def test_every_result_is_openable():
    """Whatever goes in, what comes out names a scheme a browser understands."""
    for raw in ["", "  ", "x.com", "localhost:1", "about:blank", r"C:\tmp\p.html",
                "//cdn.x.com/a.js", "HTTP://X.COM", "mailto:a@b.com"]:
        out = normalize_url(raw)
        assert config._SCHEME_RE.match(out), f"{raw!r} -> {out!r} has no scheme"
        assert out.split(":", 1)[0] in config.URL_SCHEMES, f"{raw!r} -> {out!r}"


def test_the_call_sites_use_the_helper():
    """browser.open_url and the two tool handlers must not grow their own copy
    of the "https://" + url rule again."""
    import pathlib
    root = pathlib.Path(config.PACKAGE_DIR)
    for name in ("browser.py", "desktop.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert '"https://" + url' not in src, f"{name} still prepends https:// by hand"
        assert "config.normalize_url(" in src, f"{name} does not use config.normalize_url"
