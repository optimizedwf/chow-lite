"""ADK runtime armor — hermetic tests for ADKAgentNode failure/success paths.

No GEMINI_API_KEY, no real ADK: the runner is stubbed (fake event stream),
so we can pin the loud-failure contract:
  - empty stream           -> RuntimeError (never a silent pass / fake SHIP)
  - runner raises 3x       -> last error surfaced
  - runner raises, then ok -> success (transient retry works)
  - success                -> agent_output.md artifact + final_text/function_calls
Sessions are created once per job (dedupe) even when the runner is stubbed.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nine.runtime.adk_runtime import (
    ADKAgentNode,
    make_adk_node,
    register_adk_agents,
)


class _Part:
    def __init__(self, text=None, function_call=None):
        self.text = text
        self.function_call = function_call


class _Content:
    def __init__(self, parts=None):
        self.parts = parts or []


class _Event:
    def __init__(self, is_final_response=False, content=None):
        self.is_final_response = is_final_response
        self.content = content


def _make_node(runner) -> ADKAgentNode:
    """Construct an ADKAgentNode WITHOUT importing google.adk (hermetic)."""
    node = object.__new__(ADKAgentNode)
    node.agent = None
    node.app_name = "nine"
    node.runner = runner
    node._created_sessions = set()
    return node


class _FakeRunner:
    def __init__(self, sequence):
        # sequence: list of callables(events_out) or Exception or list[Event]
        self._seq = list(sequence)
        self.calls = 0
        self.session_service = _FakeSessionService()

    def run(self, **kwargs):
        self.calls += 1
        item = self._seq.pop(0) if self._seq else []
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(**kwargs)
        return item


class _FakeSessionService:
    def __init__(self):
        self.created = []

    async def create_session(self, app_name=None, user_id=None, session_id=None):
        self.created.append((app_name, user_id, session_id))


def _run(node, task="hello", job_id="j1"):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        return node({"task": task, "job_id": job_id}, Path(d)), Path(d)


def test_empty_stream_raises_loud(tmp_path):
    node = _make_node(_FakeRunner([[]]))
    node._empty_backoff_s = 0
    with pytest.raises(RuntimeError, match="no output"):
        node({"task": "hi", "job_id": "j1"}, tmp_path)


def test_empty_stream_retried_with_backoff_then_success(tmp_path):
    # ADK swallows Gemini free-tier 429s into EMPTY streams (no exception),
    # so an empty stream must be retried with backoff, not raised at once.
    ev = _Event(is_final_response=True, content=_Content(parts=[_Part(text="done")]))
    node = _make_node(_FakeRunner([[], [], [ev]]))
    node._empty_backoff_s = 0
    out = node({"task": "hi", "job_id": "j1"}, tmp_path)
    assert node.runner.calls == 3
    assert out["output"] == "done"


def test_empty_stream_three_times_raises_with_retry_count(tmp_path):
    node = _make_node(_FakeRunner([[], [], []]))
    node._empty_backoff_s = 0
    with pytest.raises(RuntimeError, match="no output") as ei:
        node({"task": "hi", "job_id": "j1"}, tmp_path)
    assert node.runner.calls == 3
    assert "empty stream x3" in str(ei.value)


def test_no_final_text_no_tool_calls_raises(tmp_path):
    # events exist but carry no text and no function calls
    ev = _Event(is_final_response=True, content=_Content(parts=[_Part(text=None)]))
    node = _make_node(_FakeRunner([[ev]]))
    with pytest.raises(RuntimeError, match="no output"):
        node({"task": "hi", "job_id": "j1"}, tmp_path)


def test_runner_raises_three_times_surfaces_error(tmp_path):
    err = RuntimeError("429 RESOURCE_EXHAUSTED")
    node = _make_node(_FakeRunner([err, err, err]))
    with pytest.raises(RuntimeError, match="429"):
        node({"task": "hi", "job_id": "j1"}, tmp_path)
    assert node.runner.calls == 3


def test_transient_retry_then_success(tmp_path):
    ev = _Event(is_final_response=True, content=_Content(parts=[_Part(text="done")]))
    node = _make_node(_FakeRunner([RuntimeError("503"), RuntimeError("503"), [ev]]))
    out = node({"task": "hi", "job_id": "j1"}, tmp_path)
    assert node.runner.calls == 3
    assert out["output"] == "done"


def test_success_writes_artifact_and_records_calls(tmp_path):
    fc = _Part(function_call=type("FC", (), {"name": "lookup", "args": {"t": "AAPL"}})())
    ev1 = _Event(content=_Content(parts=[fc]))
    ev2 = _Event(is_final_response=True, content=_Content(parts=[_Part(text="price is 212")]))
    node = _make_node(_FakeRunner([[ev1, ev2]]))
    out = node({"task": "hi", "job_id": "j1"}, tmp_path)
    assert out["output"] == "price is 212"
    assert out["function_calls"] == ['lookup({"t": "AAPL"})']
    artifact = tmp_path / "agent_output.md"
    assert artifact.exists()
    text = artifact.read_text()
    assert "price is 212" in text
    assert "lookup" in text


def test_session_created_once_per_job(tmp_path):
    ev = _Event(is_final_response=True, content=_Content(parts=[_Part(text="ok")]))
    node = _make_node(_FakeRunner([[ev], [ev], [ev]]))
    node({"task": "a", "job_id": "j1"}, tmp_path)
    node({"task": "b", "job_id": "j1"}, tmp_path)
    assert len(node.runner.session_service.created) == 1
    # a second job id creates a second session
    node({"task": "c", "job_id": "j2"}, tmp_path)
    assert len(node.runner.session_service.created) == 2


def test_make_adk_node_spec_shape():
    spec = make_adk_node(type("A", (), {"name": "bob", "description": "desc"})(),
                         description="ADK agent step")
    assert spec["id"] == "bob"
    assert spec["kind"] == "subagent"
    assert spec["description"] == "ADK agent step"
    assert callable(spec["run"])


def test_register_adk_agents_registers_catalog():
    calls = {}

    class _Router:
        def register(self, workflow_id=None, keywords=None, description=None):
            calls[workflow_id] = (keywords, description)

    agents = [
        type("A", (), {"name": "alpha", "description": "alpha desc"})(),
        type("B", (), {"name": "beta", "description": ""})(),
    ]
    register_adk_agents(_Router(), agents)
    assert calls == {
        "alpha": (["alpha"], "alpha desc"),
        "beta": (["beta"], "ADK agent"),
    }


