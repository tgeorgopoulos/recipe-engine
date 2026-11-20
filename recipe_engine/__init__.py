"""Recipe Engine: deterministic, verified, zero-token replay of desktop and browser
workflows that Claude has already performed once through this engine's own tools.

Nothing in this package plans, prompts, or calls a language model. The invariant
self-test (`python -m recipe_engine.invariants --self-test`) proves that stays true.
"""
__version__ = "0.1.0"
