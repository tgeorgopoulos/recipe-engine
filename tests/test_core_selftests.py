"""The package's own self-tests, collected by pytest so CI runs them.

Each module keeps a `_selftest()` that needs no model, no browser, no desktop:
`recipes` drives the full browser replay loop against a fake page, `engine` proves
the single desktop owner, `safety` the credential and destructive gates, and
`invariants` mutates copies of the protected files and requires every mutation to
be caught (a mutation whose pinned text has drifted counts as a failure).
"""
from __future__ import annotations

from recipe_engine import engine, invariants, recipes, safety


def test_safety_selftest():
    safety._selftest()


def test_engine_input_lock_selftest():
    engine._selftest()


def test_recipes_selftest_runs_the_fake_page_replay_loop():
    recipes._selftest()


def test_invariants_intact_on_the_real_tree():
    assert invariants.check() == []


def test_invariants_selftest_detects_every_mutation():
    assert invariants._selftest() == 0
