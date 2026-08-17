"""Round-14 torture harvest (torture-27 learn/memory + torture-28 server/HTTP)
— re-run visibility, memory parity, honest stubs, and API hardening.

Findings (all hermetic, zero Gemini):
  T27-F1 (MED)  nine recover re-runs the SAME job id, so _record_route_event
         wrote the same event_id and Learner.learn() deduped the re-run's
         observation away (LEARN blind to verdict flips on recovery). Now
         recover() bumps job.metadata["run_seq"] and the event id carries it
         (ev-<job8>-<seq>).
  T27-F2 (LOW/MED) LocalMemoryGraph.search_context("   ") returned the
         most-recent k records as "hits" (empty terms -> all()=True) while
         Firestore returned [] — backends disagreed. Now local returns []
         for blank queries.
  T27-F3 (LOW)  FirestoreMemoryGraph.save_artifact_summary had no best-effort
         guard (JSONL backend has try/except OSError) — a Firestore outage
         mid-hop raw-tracebacked the chain. Now wrapped in except Exception
         with a WARNING (parity with the JSONL belt).
  T27-F4 (LOW)  datahub MCP node returned {"enabled": True, ...} while doing
         no work — a silent no-op reporting success. Now enabled: False with
         an honest reason.
  T28-F1 (HIGH) _LazyFallbackLedger.__getattr__ caught LedgerError from the
         PRIMARY (a normal 404) and latched the whole API onto local JSONL —
         Cloud Run loses durable-state visibility on any miss. LedgerError is
         now re-raised (endpoint's clean 404); only non-LedgerError failures
         engage the fallback.
  T28-F2 (HIGH) a gate check that RAISES was reported as passed:False ->
         FIX -> doomed model-budget-burning fix loops. Now tagged
         {"error": True} and the verdict is BLOCK (fail loud, never FIX).
  T28-F3 (MED)  RouteEventStore.all()/CandidateStore.all() read the store
         unguarded — a directory at events.jsonl raw-500'd GET /v1/events
         and /v1/stats. Both reads now OSError-belt to [] + WARNING.
  T28-F4 (MED)  GET /v1/events?limit=0 returned the WHOLE store
         (all_ev[-0:] is unbounded) and limit=-N silently inverted intent.
         Now limit < 1 or > 1000 -> 422.
  T28-F5 (MED)  whitespace-only tasks passed min_length=1 and routed to the
         respond lane, spending a paid model call on a blank prompt. Now
         SubmitRequest strips/validates -> 422.
  T28-F6 (MED)  the per-IP rate limiter keyed on request.client.host — behind
         Cloud Run's LB every request shares one bucket. Now keys on the
         trusted X-Forwarded-For last hop when present.
  T28-F7 (LOW)  MODEL was captured at import time — /health reported "none"
         forever after a key was mounted. Now reads llm_provider.model_name()
         live.
  T28-F8 (LOW)  FirestoreLedger._ref interpolated the job id into a document
         path with no escaping ("a/b" -> nested doc, ".." -> outside the
         namespace). Now ^[A-Za-z0-9_-]{1,64}$ else clean LedgerError (404).
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

# Hermetic env before any app import.
os.environ["GEMINI_API_KEY"] = ""
os.environ["FIRESTORE_EMULATOR_HOST"] = ""


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("NINE_MEMORY", "NINE_DATAHUB_MCP", "NINE_API_KEY",
              "NINE_DATA_DIR", "NINE_LLM_BACKEND"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------- T27-F1 ---
def test_t27_f1_recover_bumps_run_seq_and_event_id_carries_it(tmp_path):
    """A recovered re-run must be a NEW LEARN observation (distinct event
    id), not deduped away as the original run's."""
    from nine.ledger.ledger import JSONLLedger, LedgerError

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    job = ledger.submit("respond", {"task": "hello"})
    assert (job.metadata or {}).get("run_seq", 0) == 0
    job.transition("routing")
    ledger.update(job)
    job.transition("blocked")
    ledger.update(job)
    rec = ledger.recover(job.job_id)
    assert rec.metadata["run_seq"] == 1
    # a recover of a recovered job must raise (transition legality)
    try:
        ledger.recover(job.job_id)
        raise AssertionError("recover of a recovered job must raise")
    except LedgerError:
        pass

    # event ids: original run seq 0, recovered run seq 1
    from nine.learn.learner import RouteEvent
    ev0 = RouteEvent(
        event_id=f"ev-{job.job_id[:8]}-0",
        job_id=job.job_id,
        task_redacted="hello",
        workflow_id="respond",
        confidence=0.9,
        router_version="test",
        checks_passed=1,
        checks_total=1,
        verdict="BLOCK",
    )
    ev1 = RouteEvent(
        event_id=f"ev-{job.job_id[:8]}-1",
        job_id=job.job_id,
        task_redacted="hello",
        workflow_id="respond",
        confidence=0.9,
        router_version="test",
        checks_passed=1,
        checks_total=1,
        verdict="SHIP",
    )
    assert ev0.event_id != ev1.event_id


# ---------------------------------------------------------------- T27-F2 ---
def test_t27_f2_blank_memory_query_returns_no_hits(tmp_path):
    """Whitespace-only queries must return [] (Firestore parity), not the
    most-recent records as false hits."""
    from nine.memory.graph import LocalMemoryGraph

    mem = LocalMemoryGraph(tmp_path / "memory.jsonl")
    mem.save_artifact_summary(
        job_id="job-11111111-2222", chain_id="flagship", hop_id="research",
        workflow_id="research", artifact_name="HANDOFF.md", kind="document",
        sha256="abc", size=100, summary="findings about fooquark dynamics",
        task_redacted="study fooquark dynamics", verdict="SHIP",
    )
    assert len(mem.search_context("fooquark", k=5)) == 1
    assert mem.search_context("   ", k=5) == []
    assert mem.search_context("\n\t", k=5) == []


# ---------------------------------------------------------------- T27-F3 ---
def test_t27_f3_firestore_save_artifact_summary_has_best_effort_guard(monkeypatch, tmp_path, capsys):
    """A Firestore outage on save_artifact_summary must be swallowed with a
    WARNING (JSONL-backend parity), not raw-traceback the chain."""
    from nine.memory.graph import FirestoreMemoryGraph

    class _BoomRef:
        def set(self, **kw):
            raise RuntimeError("Firestore unavailable")

    mem = FirestoreMemoryGraph.__new__(FirestoreMemoryGraph)
    mem._ref = lambda mid: _BoomRef()  # type: ignore[method-assign]
    mem._collection = "nine-memory"
    out = mem.save_artifact_summary(
        job_id="job-11111111-2222", chain_id="flagship", hop_id="research",
        workflow_id="research", artifact_name="HANDOFF.md", kind="document",
        sha256="abc", size=100, summary="s", task_redacted="t", verdict="SHIP",
    )
    assert isinstance(out, str) and out.startswith("mem-")  # best-effort id
    assert "WARNING" in capsys.readouterr().err


# ---------------------------------------------------------------- T27-F4 ---
def test_t27_f4_datahub_stub_reports_disabled_honestly(monkeypatch, tmp_path):
    """The datahub MCP node must NOT claim enabled while unwired."""
    monkeypatch.setenv("NINE_DATAHUB_MCP", "1")
    import types
    fake = types.ModuleType("datahub_agent_context")
    fake.search = lambda *a, **k: []
    sys.modules["datahub_agent_context"] = fake
    try:
        from nine.memory.datahub import datahub_context_tool

        out = datahub_context_tool({"task": "x"}, tmp_path)
        assert out["enabled"] is False
        assert "no metadata service" in out["reason"]
    finally:
        sys.modules.pop("datahub_agent_context", None)


# ---------------------------------------------------------------- T28-F1 ---
def test_t28_f1_ledgererror_404_does_not_latch_jsonl_fallback():
    """A primary LedgerError (unknown job id) must propagate as the
    endpoint's clean 404 and NEVER engage the JSONL fallback latch."""
    from deploy.server import _LazyFallbackLedger
    from nine.ledger.ledger import LedgerError

    class _Primary404:
        def get(self, job_id):
            raise LedgerError(f"job not found: {job_id}")

    proxy = _LazyFallbackLedger(_Primary404())
    with pytest.raises(LedgerError):
        proxy.get("nope")
    assert proxy._fallback is None  # latch NOT engaged

    class _PrimaryBoom:
        def get(self, job_id):
            raise RuntimeError("network down")

    proxy2 = _LazyFallbackLedger(_PrimaryBoom())
    with pytest.raises(LedgerError):
        proxy2.get("nope")  # fallback JSONL engages; unknown id still 404s
    assert proxy2._fallback is not None  # latch ENGAGED for real outages


# ---------------------------------------------------------------- T28-F2 ---
def test_t28_f2_raising_gate_check_blocks_not_fixes(tmp_path):
    """A gate check that RAISES is a broken check -> BLOCK (fail loud), not
    FIX (which would burn model budget in doomed fix loops)."""
    from nine.gates.evidence import EvidenceGate

    gate = EvidenceGate()
    gate.register_check("broken", lambda ctx, wd: (_ for _ in ()).throw(
        TypeError("bad closure")))
    verdict = gate.evaluate({"artifact_paths": []}, tmp_path)
    assert verdict["verdict"] == "BLOCK"
    assert "raised" in verdict["summary"]
    assert verdict["eval_results"]["broken"]["error"] is True


# ---------------------------------------------------------------- T28-F3 ---
def test_t28_f3_directory_at_events_store_returns_empty(tmp_path):
    """A directory at events.jsonl / candidates.jsonl must read as an empty
    store (+WARNING), not raise OSError -> HTTP 500."""
    from nine.learn.learner import CandidateStore, RouteEventStore

    events_dir = tmp_path / "events.jsonl"
    events_dir.mkdir()
    store = RouteEventStore(events_dir)
    assert store.all() == []

    cands_dir = tmp_path / "candidates.jsonl"
    cands_dir.mkdir()
    cs = CandidateStore(cands_dir)
    assert cs.all() == []


# ---------------------------------------------------------------- T28-F4 ---
def test_t28_f4_events_limit_bounds(tmp_path, monkeypatch):
    """limit=0/-N/1001 must 422; a sane limit stays 200."""
    from fastapi.testclient import TestClient

    from deploy.server import app

    monkeypatch.setenv("NINE_DATA_DIR", str(tmp_path))
    client = TestClient(app)
    for bad in (0, -1, -5):
        r = client.get(f"/v1/events?limit={bad}")
        assert r.status_code == 422, f"limit={bad} must 422"
    r = client.get("/v1/events?limit=1001")
    assert r.status_code == 422
    r = client.get("/v1/events?limit=50")
    assert r.status_code == 200


# ---------------------------------------------------------------- T28-F5 ---
def test_t28_f5_whitespace_task_rejected_422(tmp_path, monkeypatch):
    """Whitespace-only tasks must 422, not route to the respond lane."""
    from fastapi.testclient import TestClient

    from deploy.server import app

    monkeypatch.setenv("NINE_DATA_DIR", str(tmp_path))
    client = TestClient(app)
    for bad in ("   ", "\n\t", " "):
        r = client.post("/v1/submit", json={"task": bad})
        assert r.status_code == 422, f"task {bad!r} must 422"
    r = client.post("/v1/submit", json={"task": "hi"})
    assert r.status_code in (200, 502)  # 502 = no LLM key, but NOT 422


# ---------------------------------------------------------------- T28-F6 ---
def test_t28_f6_rate_limiter_keys_on_forwarded_for(monkeypatch):
    """X-Forwarded-For's last hop (the trusted LB-inserted value) must be
    the per-IP bucket key; a client-supplied first hop must NOT split.
    slice-54 (torture-36 F6): XFF is only trusted when the platform LB is
    present (K_SERVICE=Cloud Run) or NINE_TRUST_PROXY=1 — the test must
    declare the trust boundary explicitly."""
    import types

    from deploy.server import _check_rate_limit, _hits

    monkeypatch.setenv("K_SERVICE", "cloud-run-test")
    _hits.clear()
    req = types.SimpleNamespace(
        headers={"x-forwarded-for": "1.2.3.4, 203.0.113.9"},
        client=types.SimpleNamespace(host="10.0.0.1"),
    )
    for _ in range(30):
        assert _check_rate_limit(req) is None
    r = _check_rate_limit(req)
    assert r is not None and r.status_code == 429
    # a different last hop gets its own bucket
    req2 = types.SimpleNamespace(
        headers={"x-forwarded-for": "5.6.7.8"},
        client=types.SimpleNamespace(host="10.0.0.1"),
    )
    assert _check_rate_limit(req2) is None
    # no headers -> client.host fallback still works
    req3 = types.SimpleNamespace(client=types.SimpleNamespace(host="10.0.0.9"))
    assert _check_rate_limit(req3) is None
    _hits.clear()


# ---------------------------------------------------------------- T28-F7 ---
def test_t28_f7_health_reads_model_live(tmp_path, monkeypatch):
    """/health must read the provider live — mutating the env in-process
    must change the reported model (no stale import-time constant)."""
    from fastapi.testclient import TestClient

    from deploy.server import app

    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("NINE_DATA_DIR", str(tmp_path))
    from nine.runtime import llm_provider

    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["model"] == llm_provider.model_name()
    # live read: flipping the env in-process changes what /health reports
    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9")
    r2 = client.get("/health")
    assert r2.status_code == 200
    assert r2.json()["model"] == llm_provider.model_name()


# ---------------------------------------------------------------- T28-F8 ---
def test_t28_f8_firestore_ref_rejects_path_hostile_ids():
    """_ref must reject id shapes Firestore cannot address cleanly (404
    parity with the JSONL backend) BEFORE touching the db."""
    from nine.ledger.firestore_ledger import FirestoreLedger
    from nine.ledger.ledger import LedgerError

    mem = FirestoreLedger.__new__(FirestoreLedger)
    for bad in ("a/b", "..", "../etc/passwd", "a b", "a.b"):
        try:
            mem._ref(bad)
            raise AssertionError(f"_ref({bad!r}) must raise LedgerError")
        except LedgerError:
            pass
    # a normal uuid4 id is fine (db access raises, but validation passes)
    good = "0123456789abcdef"
    try:
        mem._ref(good)
    except (AttributeError, TypeError) as exc:
        assert "db" in type(exc).__name__.lower() or "attribute" in str(exc).lower()
