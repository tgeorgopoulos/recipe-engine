# Tests

Three tiers, selected by marker. A plain `pytest` runs the first tier only, so it
never touches your screen or your browser.

| Run | What it drives | Marker |
|---|---|---|
| `pytest` | nothing real: fake keyboard, fake pages, fake UI trees, a loopback HTTP fixture | (default: `not slow and not integration and not packaging`) |
| `pytest -m slow` | real Chrome on a throwaway profile (downloads, saved logins, the shim against the real host) | `slow` |
| `pytest -m integration` | the real desktop: Notepad, Calculator, a WinForms password box, an in-place background replay, a real Esc | `integration` |
| `pytest -m packaging` | `mcpb validate`, `claude plugin validate --strict`, the Node shim | `packaging` |

Two gates sit outside pytest on purpose, so they cannot be skipped by a marker:

```
python -m recipe_engine.invariants --self-test    # 17 invariants, 43 source mutations must all be caught
python tests/reliability_study.py --runs 50       # record once, replay 50x: >= 95 % ok, median < 10 s
```

## What the integration tier needs

An unlocked, interactive Windows session with nothing important in the
foreground: the tests open their own Notepad / Calculator windows, drive them,
and close only those windows (Store Notepad is one process for every tab, so a
test never kills the process). `recipe-engine check` says whether the session
qualifies; CI runs it first and fails loudly rather than skipping.

If your own window holds the foreground lock (a game-input service does this on
some machines), the background-replay test still runs but records that it could
not set up the "user window in front" premise.

## DPI coverage is honest, not real

Real 1080p/100 %, 1440p/125 % and 4K/150 % displays are not reproducible on one
machine or on a CI runner. `test_uia_resolver.py` covers the three display stamps
and the coordinate arithmetic synthetically; the live tests run at whatever
display the machine has and report it in the reliability study.

## Fixture site

`tests/serve_fixture.py A|B` serves `tests/fixtures/site_a` (nav link "Invoices")
or `site_b` (the same link renamed "Billing", so a recipe recorded on A breaks on
B the way a real portal update would). It also serves `/report.csv`,
`/empty.csv` and `/slow-download` for the four download outcomes, and
`/login.html` with a real password field.
