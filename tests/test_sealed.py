"""Recipes at rest are sealed with DPAPI (this Windows user, this machine); the test
suite normally runs unsealed (conftest sets RECIPE_ENGINE_UNSEALED=1), so these
tests flip the switch explicitly to prove the sealed path."""
from __future__ import annotations

import json
import sys

import pytest

from recipe_engine import recipes, sealed

win = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows only")


@pytest.fixture
def sealed_mode(monkeypatch):
    monkeypatch.delenv("RECIPE_ENGINE_UNSEALED", raising=False)
    assert sealed.sealed() is (sys.platform == "win32")


def _recipe():
    return recipes.Recipe(id="seal-test", command_normalized="export the weekly report", name="weekly-report",
                          steps=[{"surface": "browser", "action": "click", "selector": "role=link[name='Export CSV']",
                                  "expect": "url=https://portal.example/reports"}])


@win
def test_a_saved_recipe_is_unreadable_without_dpapi(sealed_mode, tmp_path):
    p = recipes.save(_recipe())
    raw = p.read_bytes()
    assert raw.startswith(sealed.MAGIC_DPAPI)
    assert b"Export CSV" not in raw and b"weekly-report" not in raw, "recipe content is visible in the file"
    assert not sealed.is_plaintext_on_disk(p)
    back = recipes.load("seal-test")
    assert back is not None and back.steps[0]["selector"] == "role=link[name='Export CSV']"
    recipes.forget("seal-test")


def test_unsealed_mode_writes_a_marked_plaintext_file(monkeypatch):
    monkeypatch.setenv("RECIPE_ENGINE_UNSEALED", "1")
    p = recipes.save(_recipe())
    raw = p.read_bytes()
    assert raw.startswith(sealed.MAGIC_PLAIN) and b"Export CSV" in raw
    assert sealed.is_plaintext_on_disk(p)
    assert recipes.load("seal-test").name == "weekly-report"
    recipes.forget("seal-test")


def test_legacy_plaintext_files_still_load_and_are_resealed_on_save(tmp_path, monkeypatch):
    p = recipes.path_for("legacy-1")
    p.write_text(json.dumps(_recipe().to_dict() | {"id": "legacy-1"}), encoding="utf-8")   # pre-sealing layout
    r = recipes.load("legacy-1")
    assert r is not None and r.name == "weekly-report"
    recipes.save(r)
    assert p.read_bytes()[:8] in (sealed.MAGIC_DPAPI, sealed.MAGIC_PLAIN)
    recipes.forget("legacy-1")


def test_a_foreign_or_corrupt_file_is_skipped_not_crashed(tmp_path):
    p = recipes.path_for("junk-1")
    p.write_bytes(b"XXNOTOURS" + b"\x00" * 20)
    assert recipes.load("junk-1") is None
    assert all(r.id != "junk-1" for r in recipes.load_all())
    with pytest.raises(OSError):
        sealed.unseal_bytes(b"XXNOTOURS")
    p.unlink()


def test_writes_are_atomic_no_tmp_left_behind(tmp_path):
    p = sealed.write_sealed_json(tmp_path / "x.json", {"a": 1})
    assert sealed.read_sealed_json(p) == {"a": 1}
    assert not list(tmp_path.glob("*.tmp"))
