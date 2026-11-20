"""Make `import recipe_engine` work from a plain checkout (no `pip install -e .`)
and keep every test's data on a throwaway directory so no test can touch the real
%LOCALAPPDATA%\\RecipeEngine store, token or browser profile."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Must be set BEFORE recipe_engine.config is imported anywhere: the data dir is
# resolved once at import, on purpose (same contract as the old VOICE_AGENT_RECIPES_DIR).
os.environ.setdefault("RECIPE_ENGINE_DATA_DIR", tempfile.mkdtemp(prefix="recipe-engine-tests-"))
os.environ.setdefault("RECIPE_ENGINE_UNSEALED", "1")
