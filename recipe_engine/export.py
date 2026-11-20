"""Export / import: a recipe as signed JSON for another machine.

What travels: name, description, surface, steps (selectors, UIA identities,
{{parameters}}, expectations), params, app hints, the recorded display stamp,
background_capable, schema. What NEVER travels: run counts, history, last-run
times, quarantine state, cookies, browser storage, tokens, or any text the
credential regex matches - a recipe holding one is refused at export AND at
import. The signature is HMAC-SHA256 with this install's DPAPI-sealed export
key; an importer with a different key gets signature_ok=false (it still
imports - the safety checks are re-run, and the recipe starts with zero runs
and no quarantine, exactly like a freshly recorded one).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recipe_engine import config, recipes, safety, sealed
from recipe_engine.config import log
from recipe_engine.safety import _SECRET_RE

FORMAT = "recipe-engine/1"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
# keys a recipe may carry across machines
_PORTABLE = ("name", "description", "surface", "steps", "params", "app_hints", "display", "background_capable",
             "schema", "command_normalized", "command_variants", "preconditions", "created")
# key names that must never appear anywhere in an exported or imported payload
_FORBIDDEN_KEY_RE = re.compile(r"(cookie|storage|session|token|secret|credential|auth)", re.I)


# ---------------------------------------------------------------- key
def export_key() -> bytes:
    p = Path(config.EXPORT_KEY_FILE)
    if p.exists():
        try:
            return sealed.unseal_bytes(p.read_bytes())
        except OSError as e:
            log("EXPORT", f"export key unreadable ({e!r}); a new one is generated - older exports will show signature_ok=false")
    key = os.urandom(32)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(sealed.seal_bytes(key))
    os.replace(tmp, p)
    try:
        from recipe_engine.auth import _restrict_acl
        _restrict_acl(p)
    except Exception:
        pass
    return key


def key_id(key: bytes | None = None) -> str:
    return hashlib.sha256(key or export_key()).hexdigest()[:12]


def _canonical(payload: dict[str, Any]) -> bytes:
    body = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _sign(payload: dict[str, Any], key: bytes | None = None) -> str:
    return hmac.new(key or export_key(), _canonical(payload), hashlib.sha256).hexdigest()


def _walk_keys(obj: Any):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


def _safety_problems(recipe_dict: dict[str, Any]) -> str:
    """'' if the portable recipe is safe to move; otherwise the reason."""
    blob = json.dumps(recipe_dict, ensure_ascii=False)
    text = " ".join([str(recipe_dict.get("name") or ""), str(recipe_dict.get("description") or "")]
                    + [str(s.get("value") or "") + " " + str(s.get("selector") or "") + " " + str((s.get("uia") or {}).get("name") or "")
                       for s in recipe_dict.get("steps") or [] if isinstance(s, dict)])
    if safety.is_destructive(text):
        return f"refused: {safety.destructive_reason(text)} - a recipe that sends, submits, buys or deletes is never moved"
    if _SECRET_RE.search(blob):
        return "refused: the recipe contains credential-looking text; nothing that matches the credential regex leaves this machine"
    bad = sorted({k for k in _walk_keys(recipe_dict) if _FORBIDDEN_KEY_RE.search(k)})
    if bad:
        return f"refused: the payload carries forbidden keys {bad} (cookies, storage and tokens never travel)"
    return ""


# ---------------------------------------------------------------- export
def export_recipe(name: str) -> dict[str, Any]:
    r = recipes.load_by_name(name)
    if r is None:
        return {"status": "failed", "failure_reason": f"no_such_recipe: {name!r}"}
    d = r.to_dict()
    for k in ("runs", "history", "last_success", "last_used", "avg_duration_s", "quarantined", "consecutive_failures",
              "imported", "destructive", "id"):
        d.pop(k, None)                                    # nothing about THIS machine's runs travels
    portable = {k: d[k] for k in _PORTABLE if k in d}
    why = _safety_problems(portable)
    if why:
        return {"status": "refused", "failure_reason": why}
    key = export_key()
    payload: dict[str, Any] = {"format": FORMAT, "exported": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "key_id": key_id(key), "engine_version": __import__("recipe_engine").__version__,
                               "recipe": portable}
    payload["signature"] = _sign(payload, key)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    log("EXPORT", f"{r.name}: {len(portable.get('steps') or [])} steps, {len(text)} bytes")
    return {"status": "ok", "name": r.name, "json": text, "steps": len(portable.get("steps") or []), "key_id": payload["key_id"]}


# ---------------------------------------------------------------- import
def import_recipe(text: str, replace: bool = False) -> dict[str, Any]:
    try:
        payload = json.loads(text or "")
    except ValueError as e:
        return {"status": "failed", "failure_reason": f"not JSON: {e}"}
    if not isinstance(payload, dict) or payload.get("format") != FORMAT or not isinstance(payload.get("recipe"), dict):
        return {"status": "failed", "failure_reason": f"not a {FORMAT} export"}
    given = str(payload.get("signature") or "")
    signature_ok = bool(given) and hmac.compare_digest(given, _sign(payload))
    rd = dict(payload["recipe"])
    bad = sorted({k for k in _walk_keys(rd) if _FORBIDDEN_KEY_RE.search(k)})
    if bad:                                               # checked on the RAW payload, before anything is dropped
        return {"status": "refused", "failure_reason": f"refused: the payload carries forbidden keys {bad} (cookies, storage and tokens never travel)",
                "signature_ok": signature_ok}
    portable = {k: rd[k] for k in _PORTABLE if k in rd}
    why = _safety_problems(portable)                      # re-run here: the file may have been edited
    if why:
        return {"status": "refused", "failure_reason": why, "signature_ok": signature_ok}
    name = str(portable.get("name") or "").strip().lower()
    if not _NAME_RE.match(name):
        return {"status": "refused", "failure_reason": f"bad recipe name {name!r}: 2-64 chars of a-z, 0-9 and '-'"}
    steps = portable.get("steps")
    if not isinstance(steps, list) or not steps or not all(isinstance(s, dict) and s.get("action") for s in steps):
        return {"status": "refused", "failure_reason": "the recipe has no usable steps"}
    if portable.get("surface") not in ("browser", "desktop"):
        return {"status": "refused", "failure_reason": f"unknown surface {portable.get('surface')!r}"}
    existing = recipes.load_by_name(name)
    if existing is not None and not replace:
        return {"status": "refused", "failure_reason": f"a recipe named {name!r} already exists; forget it first or import with replace=true"}
    if existing is not None:
        recipes.forget(existing.id)
    r = recipes.Recipe(id=recipes.recipe_id(name), command_normalized=str(portable.get("command_normalized") or name.replace("-", " ")),
                       command_variants=[str(v) for v in portable.get("command_variants") or []],
                       surface=str(portable["surface"]), steps=steps, description=str(portable.get("description") or ""),
                       name=name, app_hints=[str(h) for h in portable.get("app_hints") or []],
                       params=list(portable.get("params") or []), display=portable.get("display"),
                       background_capable=bool(portable.get("background_capable")), preconditions=dict(portable.get("preconditions") or {}),
                       imported=True)                     # zero runs, no history, not quarantined: a fresh start
    recipes.save(r)
    log("IMPORT", f"{name}: {len(steps)} steps, signature_ok={signature_ok}")
    return {"status": "ok", "name": name, "signature_ok": signature_ok, "steps": len(steps), "surface": r.surface,
            "background_capable": r.background_capable,
            "note": "" if signature_ok else "signed by another install (or edited since): imported unquarantined with zero runs; the safety checks were re-run here"}
