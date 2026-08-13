"""Gemma 4 armor — hermetic failure-mode tests (no key, no network).

gemma_generate's contract: returns None on ANY failure (no key, requests
missing, HTTP error, empty candidates, exception) and the stripped joined
text on success. These tests pin that contract with a stub requests module
so the suite never needs GEMINI_API_KEY.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nine.runtime.gemma as gemma_mod


class _Resp:
    def __init__(self, status_code, payload=None, exc=None):
        self.status_code = status_code
        self._payload = payload
        self._exc = exc

    def json(self):
        if self._exc is not None:
            raise self._exc
        return self._payload


class _StubRequests:
    """Minimal stand-in for the requests module: records the POST."""

    def __init__(self, responses=None):
        # responses: list of _Resp or callable(*args, **kwargs) -> _Resp
        self.responses = list(responses or [])
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.responses:
            r = self.responses.pop(0)
            if callable(r):
                r = r(*args, **kwargs)
            return r
        return _Resp(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})


def _install_stub(monkeypatch, responses):
    stub = _StubRequests(responses)
    monkeypatch.setattr(gemma_mod, "requests", stub)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return stub


def test_no_key_returns_none_without_calling(monkeypatch):
    stub = _StubRequests()
    monkeypatch.setattr(gemma_mod, "requests", stub)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert gemma_mod.gemma_generate("hi") is None
    assert stub.calls == []


def test_requests_none_returns_none(monkeypatch):
    monkeypatch.setattr(gemma_mod, "requests", None)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert gemma_mod.gemma_generate("hi") is None


def test_http_error_returns_none(monkeypatch):
    stub = _install_stub(monkeypatch, [_Resp(429, {"error": "quota"})])
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert gemma_mod.gemma_generate("hi") is None
    assert len(stub.calls) == 1
    assert stub.calls[0][1]["headers"]["x-goog-api-key"] == "k"


def test_http_error_does_not_carry_key_in_payload(monkeypatch):
    stub = _install_stub(monkeypatch, [_Resp(500, {})])
    monkeypatch.setenv("GEMINI_API_KEY", "secret-key-xyz")
    assert gemma_mod.gemma_generate("hi") is None
    # key travels only in the x-goog-api-key header, never in the body
    body = stub.calls[0][1]["json"]
    assert "secret-key-xyz" not in repr(body)


def test_empty_candidates_returns_none(monkeypatch):
    _install_stub(monkeypatch, [_Resp(200, {"candidates": []})])
    assert gemma_mod.gemma_generate("hi") is None


def test_missing_candidates_key_returns_none(monkeypatch):
    _install_stub(monkeypatch, [_Resp(200, {})])
    assert gemma_mod.gemma_generate("hi") is None


def test_no_text_parts_returns_none(monkeypatch):
    _install_stub(monkeypatch, [_Resp(200, {"candidates": [{"content": {"parts": []}}]})])
    assert gemma_mod.gemma_generate("hi") is None


def test_exception_returns_none(monkeypatch):
    stub = _install_stub(monkeypatch, [_Resp(200, None, exc=RuntimeError("boom"))])
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert gemma_mod.gemma_generate("hi") is None
    assert len(stub.calls) == 1


def test_success_returns_joined_stripped_text(monkeypatch):
    stub = _install_stub(monkeypatch, [])
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    out = gemma_mod.gemma_generate("  hello   ")
    assert out == "ok"
    assert len(stub.calls) == 1
    url = stub.calls[0][0][0]
    assert url.startswith("https://generativelanguage.googleapis.com/v1beta/models/")
    assert gemma_mod.DEFAULT_MODEL in url


def test_custom_model_and_explicit_key(monkeypatch):
    stub = _install_stub(monkeypatch, [])
    out = gemma_mod.gemma_generate("hi", model="gemma-custom", api_key="explicit")
    assert out == "ok"
    url = stub.calls[0][0][0]
    assert "gemma-custom" in url
    assert stub.calls[0][1]["headers"]["x-goog-api-key"] == "explicit"


def test_timeout_is_passed(monkeypatch):
    stub = _install_stub(monkeypatch, [])
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    gemma_mod.gemma_generate("hi", timeout=7)
    assert stub.calls[0][1]["timeout"] == 7
