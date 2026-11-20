"""Sealed JSON at rest. Recipes (and anything else that describes what the user
does on their screen) are written through here so a file copied off the machine
is unreadable: Windows DPAPI, current user only, no dialog ever.

File layout: an 8-byte magic then the payload.
    REDPAPI1 <CryptProtectData blob of UTF-8 JSON>    the normal case
    REPLAIN1 <UTF-8 JSON>                             only when RECIPE_ENGINE_UNSEALED=1
    {  ...                                            a legacy plaintext file (pre-sealing); read, re-sealed on next save

The plaintext switch exists for the test-suite (conftest sets it) and for a
non-Windows checkout; the service never sets it. `sealed()` says which mode is on
so engine_status can report it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

MAGIC_DPAPI = b"REDPAPI1"
MAGIC_PLAIN = b"REPLAIN1"
_UNSEALED_ENV = "RECIPE_ENGINE_UNSEALED"


def sealed() -> bool:
    """True when files written now are DPAPI-sealed."""
    return sys.platform == "win32" and os.environ.get(_UNSEALED_ENV, "") not in ("1", "true", "yes")


def seal_bytes(data: bytes) -> bytes:
    if sealed():
        from recipe_engine.session_store import _dpapi
        return MAGIC_DPAPI + _dpapi(data, True)
    return MAGIC_PLAIN + data


def unseal_bytes(blob: bytes) -> bytes:
    """Any of the three layouts above. Raises OSError on a foreign or corrupt file."""
    if blob.startswith(MAGIC_DPAPI):
        if sys.platform != "win32":
            raise OSError("file was sealed with Windows DPAPI on another machine")
        from recipe_engine.session_store import _dpapi
        return _dpapi(blob[len(MAGIC_DPAPI):], False)
    if blob.startswith(MAGIC_PLAIN):
        return blob[len(MAGIC_PLAIN):]
    head = blob.lstrip()[:1]
    if head in (b"{", b"["):                                  # legacy plaintext JSON
        return blob
    raise OSError("not a recipe-engine sealed file")


def write_sealed_json(path: Path, obj: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, indent=2).encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(seal_bytes(data))
    os.replace(tmp, path)                                     # never leave a half-written recipe
    return path


def read_sealed_json(path: Path) -> Any:
    return json.loads(unseal_bytes(Path(path).read_bytes()).decode("utf-8"))


def is_plaintext_on_disk(path: Path) -> bool:
    """True if the file would be readable without DPAPI (legacy or REPLAIN1)."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    return not head.startswith(MAGIC_DPAPI)
