"""Gemma 4 helper tests — honest None contract + (keyed) live call.

gemma_generate returns None when no key/HTTP error/no candidates — it never
fabricates. Callers (flagship._teach_gemma_node) fail loud on None.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nine.runtime.gemma import gemma_generate


def test_gemma_generate_without_key_returns_none(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert gemma_generate("hi") is None


def test_gemma_generate_bad_model_returns_none(monkeypatch):
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    out = gemma_generate("hi", model="gemma-does-not-exist-xyz")
    assert out is None


def test_gemma_generate_live(monkeypatch):
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    out = gemma_generate("Reply with exactly: GEMMA-OK")
    assert out is not None
    assert "GEMMA" in out.upper()
