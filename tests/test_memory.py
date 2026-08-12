"""Semantic memory layer tests — summarize node, MemoryGraph backends, DataHub stub.

Hermetic: no GEMINI_API_KEY anywhere (deterministic extractive fallback).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

os.environ["GEMINI_API_KEY"] = ""

from nine.chains.chain import ChainExecutor
from nine.chains.flagship import research_hop, research_plan_build_review_teach
from nine.gates.evidence import required_artifact_check
from nine.ledger.ledger import JSONLLedger
from nine.memory.datahub import datahub_context_tool, datahub_tool_node
from nine.memory.graph import (
    FirestoreMemoryGraph,
    LocalMemoryGraph,
    get_memory_graph,
)
from nine.runtime.summarizer import build_summarize_node, summarize_text
from nine.runtime.workflows import Workflow, WorkflowExecutor

# ---------------------------------------------------------------- summarizer

def test_summarize_text_deterministic_fallback():
    text = "word " * 500
    summary, model = summarize_text(text, max_words=120)
    assert model == "deterministic-extractive"
    assert len(summary.split()) <= 135
    assert summary.startswith("word")


def test_summarize_node_writes_handoff_artifact(tmp_path):
    wf = Workflow(id="research")
    wf.add_node(build_summarize_node("research.md", depends_on=[]))
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = __import__("nine.gates.evidence", fromlist=["EvidenceGate"]).EvidenceGate()
    gate.register_check("handoff", required_artifact_check(["HANDOFF.md"]))
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("research", {"task": "study fooquark"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "research.md").write_text("# Findings\n" + "insight " * 300)

    res = ex.execute(wf, job, {"task": "study fooquark"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "HANDOFF.md").exists()
    names = {a["name"] for a in ledger.get(job.job_id).artifacts}
    assert "HANDOFF.md" in names
    assert "extractive" in (job_dir / "HANDOFF.md").read_text()


def test_summarize_node_missing_source_fails_job(tmp_path):
    from nine.runtime.workflows import WorkflowError

    wf = Workflow(id="research")
    wf.add_node(build_summarize_node("nope.md", depends_on=[]))
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    from nine.gates.evidence import EvidenceGate

    ex = WorkflowExecutor(ledger, EvidenceGate(), workdir=tmp_path / "work")
    job = ledger.submit("research", {"task": "x"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(WorkflowError):
        ex.execute(wf, job, {"task": "x"})
    assert job.status == "failed"


# ---------------------------------------------------------------- MemoryGraph

def test_local_memory_roundtrip_and_search(tmp_path):
    mem = LocalMemoryGraph(tmp_path / "memory.jsonl")
    mid = mem.save_artifact_summary(
        job_id="job-11111111-2222", chain_id="flagship", hop_id="research",
        workflow_id="research", artifact_name="HANDOFF.md", kind="document",
        sha256="abc", size=100, summary="findings about fooquark dynamics",
        task_redacted="study fooquark dynamics", verdict="SHIP",
    )
    assert mid.startswith("mem-job-1111")
    assert len(mem) == 1
    hits = mem.search_context("fooquark", k=5)
    assert len(hits) == 1
    assert hits[0]["hop_id"] == "research"
    assert mem.search_context("unrelated-term") == []


def test_memory_factory_modes(tmp_path, monkeypatch):
    monkeypatch.delenv("NINE_MEMORY", raising=False)
    assert isinstance(get_memory_graph(path=tmp_path / "m.jsonl"), LocalMemoryGraph)
    monkeypatch.setenv("NINE_MEMORY", "none")
    assert get_memory_graph(path=tmp_path / "m.jsonl") is None
    monkeypatch.setenv("NINE_MEMORY", "local")
    assert isinstance(get_memory_graph(path=tmp_path / "m.jsonl"), LocalMemoryGraph)


# ------------------------------------------------- FirestoreMemoryGraph (fake)

class FakeDoc:
    def __init__(self, data):
        self._data = data
        self.exists = bool(data)

    def to_dict(self):
        return self._data

    def set(self, data, merge=False):
        self._data = {**self._data, **data} if merge else data
        self.exists = True


class FakeStream:
    def __init__(self, docs):
        self._docs = docs

    def __iter__(self):
        return iter(self._docs)


class FakeCollection:
    def __init__(self):
        self.docs = {}

    def document(self, doc_id):
        if doc_id not in self.docs:
            self.docs[doc_id] = FakeDoc({})
        return self.docs[doc_id]

    def order_by(self, *a, **kw):
        return self

    def limit(self, n):
        return self

    def stream(self):
        return FakeStream(sorted(
            self.docs.values(),
            key=lambda d: d.to_dict().get("created_at", ""),
            reverse=True,
        ))


class FakeFirestore:
    def __init__(self):
        self.collections = {}

    def collection(self, name):
        if name not in self.collections:
            self.collections[name] = FakeCollection()
        return self.collections[name]


@pytest.fixture
def fake_firestore(monkeypatch):
    fake = FakeFirestore()
    import google.cloud.firestore as fs

    monkeypatch.setattr(fs, "Client", lambda *a, **kw: fake)
    return fake


def test_firestore_memory_save_and_search(fake_firestore):
    mem = FirestoreMemoryGraph(collection="nine-memory")
    mem.save_artifact_summary(
        job_id="j1", chain_id="flagship", hop_id="research",
        workflow_id="research", artifact_name="HANDOFF.md", kind="document",
        sha256="x", size=1, summary="found chromodynamics insight",
        task_redacted="study chromodynamics", verdict="SHIP",
    )
    hits = mem.search_context("chromodynamics", k=5)
    assert len(hits) == 1
    assert hits[0]["artifact_name"] == "HANDOFF.md"
    assert mem.search_context("nope") == []


# ---------------------------------------------------------------- DataHub stub

def test_datahub_tool_disabled_without_flag(monkeypatch):
    monkeypatch.delenv("NINE_DATAHUB_MCP", raising=False)
    out = datahub_context_tool({"task": "x"}, Path("/tmp"))
    assert out["enabled"] is False
    assert "NINE_DATAHUB_MCP" in out["reason"]


def test_datahub_tool_flag_but_not_installed(monkeypatch):
    monkeypatch.setenv("NINE_DATAHUB_MCP", "1")
    out = datahub_context_tool({"task": "x"}, Path("/tmp"))
    assert out["enabled"] is False
    assert "datahub-agent-context" in out["reason"]


def test_datahub_tool_node_builder():
    node = datahub_tool_node()
    assert node.id == "datahub-context"
    assert node.kind == "tool"


# ------------------------------------------------- chain wiring -> memory

class RecordingMemory:
    def __init__(self):
        self.saved = []

    def save_artifact_summary(self, **kw):
        self.saved.append(kw)
        return f"mem-{len(self.saved)}"

    def search_context(self, query, k=5):
        return []


def test_flagship_chain_records_memories_with_handoff(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    mem = RecordingMemory()
    ex = ChainExecutor(ledger, workdir=tmp_path / "work", memory=mem)

    job = ledger.submit("research-plan-build-review-teach", {"task": "build a calculator"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("build a calculator\n")

    res = ex.execute(research_plan_build_review_teach(), job, {"task": "build a calculator"})
    assert res["final"] == "SHIPPED"
    assert len(mem.saved) > 0
    hops = {s["hop_id"] for s in mem.saved}
    assert {"research", "plan", "build", "review", "teach"} <= hops
    handoff = [s for s in mem.saved if s["artifact_name"] == "HANDOFF.md"]
    assert handoff and "distilled" in handoff[0]["summary"].lower()
    assert all(s["task_redacted"] for s in mem.saved)


def test_research_hop_with_datahub_node_ships(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    hop = research_hop(include_datahub=True)
    job = ledger.submit("research", {"task": "inbox item"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("inbox item\n")

    from nine.gates.evidence import EvidenceGate
    from nine.runtime.workflows import WorkflowExecutor as WE

    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    wfex = WE(ledger, gate, workdir=tmp_path / "work", job_dir_override=job_dir)
    res = wfex.execute(hop.workflow, job, {"task": "inbox item"})
    assert res["verdict"]["verdict"] == "SHIP"
    names = {a["name"] for a in ledger.get(job.job_id).artifacts}
    assert {"research.md", "HANDOFF.md"} <= names
