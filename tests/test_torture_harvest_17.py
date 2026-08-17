"""Round-18 torture harvest tests (torture-36 — learn + memory + deploy).

Eight findings from torture-36, all FIXED in slice-54:

T36-F1 (HIGH) ADK error paths echo RAW unredacted task text into CLI
             stderr / HTTP 502 — credentials survive redact() because it
             is lexical and lossy. _safe_task_fragment never depends on
             it (paranoid token-family belt; drops to a placeholder).
T36-F2 (MED)  route-event identity used job_id[:8] — prefix collisions
             collapsed distinct jobs onto ONE event, blinding LEARN.
             Full job id now (anon-<sha> fallback for no-job events).
T36-F3 (MED)  CandidateStore.all() raw-crashed apply/revert on valid-JSON
             wrong-shape params (str) — _coerce_candidate skips them.
T36-F4 (LOW)  LocalMemoryGraph memory_id used job_id[:8]+raw artifact —
             collisions + slash-in-id + Firestore divergence. Now a
             deterministic full-job-hash + sanitized artifact.
T36-F5 (LOW)  BLOCK candidate descriptions embedded RAW fix_directive —
             now redact()ed before the durable write.
T36-F6 (LOW)  rate limiter trusted client-writable X-Forwarded-For
             outside Cloud Run; no CORS. XFF now gated on
             K_SERVICE/NINE_TRUST_PROXY; CORS allowlist default empty.
T36-F7 (LOW)  _derive_keyword mangled non-ASCII tasks ("déployer" ->
             "ployer"). \b ASCII-word scan yields "" for non-ASCII.
T36-F8 (LOW)  scan dedupe case/whitespace-sensitive. has() normalizes
             (casefold + collapse whitespace).
"""
from __future__ import annotations

import json
import types

import pytest

from nine.learn.learner import (
    CandidateStore,
    ImprovementCandidate,
    Learner,
    RouteEvent,
    RouteEventStore,
    _coerce_candidate,
    _derive_keyword,
)


# --------------------------------------------------------------- T36-F1
def test_t36_f1_adk_error_path_never_echoes_credential():
    """The two ADK RuntimeError paths must embed the redacted-safe task
    fragment — never the raw task, never a partial credential prefix."""
    from nine.runtime.adk_runtime import _safe_task_fragment

    cases = [
        "deploy api_key=sk-ABCDEF123456 to prod",
        "use AIzaSyD1234567890abcdef as key",
        "change the password to hunter2",
        "the token is ghp_ABCDEF1234567890",
        "fix the glpat-ABCDEF1234567890 bug",
        "-----BEGIN RSA PRIVATE KEY----- MIIEowIBAAKCAQEA",
    ]
    for task in cases:
        frag = _safe_task_fragment(task)
        # no credential value fragment may survive
        for bad in ("sk-ABCDEF123456", "AIzaSyD1234567890abcdef", "hunter2",
                    "ghp_ABCDEF1234567890", "glpat-ABCDEF1234567890",
                    "MIIEowIBAAKCAQEA"):
            assert bad not in frag, (task, frag)
        # benign text still shows through (error messages stay useful)
    frag_ok = _safe_task_fragment("the network timeout is too low")
    assert "network timeout" in frag_ok

# --------------------------------------------------------------- T36-F2
def test_t36_f2_event_id_uses_full_job_id_no_prefix_collision(tmp_path):
    """Distinct jobs sharing an 8-char prefix must yield distinct event
    ids (full job id + run_seq), so LEARN never collapses them."""
    store = RouteEventStore(tmp_path / "events.jsonl")
    # same 8-char prefix, distinct full ids
    for jid in ("abcdef1234567890", "abcdef12AAAAAAA"):
        store.record(RouteEvent(
            event_id=f"ev-{jid}-0", job_id=jid,
            task_redacted=f"task {jid}", workflow_id="build",
            confidence=0.5, router_version="v1", verdict="BLOCK",
            checks_passed=0, checks_total=2,
            fix_directive=f"fix {jid}",
        ))
    events = store.all()
    assert len(events) == 2
    assert len({e.event_id for e in events}) == 2
    # two colliding ids used to dedupe into one candidate
    learner = Learner(store)
    cands = learner.learn()
    assert len(cands) == 2, "prefix-colliding jobs must not dedupe"

def test_t36_f2_source_no_eight_char_job_prefix_in_event_ids():
    """Event-identity construction must not slice job ids to 8 chars.
    (cli.py's discover list prints job_id[:8] for HUMAN display — that is
    presentation, not identity, and is allowed.)"""
    import inspect

    import nine.chains.chain as chain_mod
    import nine.cli as cli_mod

    src = inspect.getsource(cli_mod) + inspect.getsource(chain_mod)
    # every event_id= construction must use the FULL job id
    for m in (
        'event_id=f"ev-{job.job_id[:8]',
        'event_id=f"ev-{hop_job.job_id[:8]',
    ):
        assert m not in src, m

# --------------------------------------------------------------- T36-F3
def test_t36_f3_wrong_shape_params_candidate_is_skipped(tmp_path):
    """A valid-JSON candidate with params as a STRING must be skipped by
    CandidateStore.all() — not constructed (dataclasses never
    type-check) and not raw-crashing apply/revert."""
    path = tmp_path / "candidates.jsonl"
    path.write_text(json.dumps({
        "candidate_id": "cand-bad1", "kind": "keyword",
        "description": "add foo", "evidence": ["ev-1"],
        "status": "pending", "params": "garbage-string",
    }) + "\n")
    store = CandidateStore(path)
    assert store.all() == []
    assert store.get("cand-bad1") is None

def test_t36_f3_missing_params_defaults_to_empty_dict(tmp_path):
    path = tmp_path / "candidates.jsonl"
    path.write_text(json.dumps({
        "candidate_id": "cand-ok", "kind": "keyword",
        "description": "add foo", "evidence": ["ev-1"],
        "status": "pending",
    }) + "\n")
    store = CandidateStore(path)
    got = store.all()
    assert len(got) == 1
    assert got[0].params == {}

# --------------------------------------------------------------- T36-F4
def test_t36_f4_memory_id_collision_free(tmp_path):
    from nine.memory.graph import LocalMemoryGraph

    mem = LocalMemoryGraph(tmp_path / "memory.jsonl")
    mid1 = mem.save_artifact_summary(
        job_id="job-abcdef12-1", chain_id="flagship", hop_id="research",
        workflow_id="research", artifact_name="HANDOFF.md",
        kind="document", sha256="abc", size=100,
        summary="findings", task_redacted="t", verdict="SHIP",
    )
    mid2 = mem.save_artifact_summary(
        job_id="job-abcdef12-2", chain_id="flagship", hop_id="research",
        workflow_id="research", artifact_name="HANDOFF.md",
        kind="document", sha256="abc", size=100,
        summary="findings", task_redacted="t", verdict="SHIP",
    )
    assert mid1 != mid2, "shared 8-char prefix must not collide"
    assert "/" not in mid1, "artifact subdir must not embed slashes"
    # deterministic: same job+artifact -> same id
    mid3 = mem.save_artifact_summary(
        job_id="job-abcdef12-1", chain_id="flagship", hop_id="research",
        workflow_id="research", artifact_name="HANDOFF.md",
        kind="document", sha256="abc", size=100,
        summary="findings", task_redacted="t", verdict="SHIP",
    )
    assert mid1 == mid3
    # subdir artifact sanitized, no slash
    mid4 = mem.save_artifact_summary(
        job_id="job-abcdef12-1", chain_id="flagship", hop_id="research",
        workflow_id="research", artifact_name="solution/main.py",
        kind="document", sha256="abc", size=100,
        summary="findings", task_redacted="t", verdict="SHIP",
    )
    assert "/" not in mid4 and mid4 != mid1

# --------------------------------------------------------------- T36-F5
def test_t36_f5_block_candidate_redacts_fix_directive(tmp_path):
    """BLOCK candidate descriptions must redact credential-shaped
    fix_directives before persisting them."""
    store = RouteEventStore(tmp_path / "events.jsonl")
    store.record(RouteEvent(
        event_id="ev-f5", job_id="j5", task_redacted="deploy thing",
        workflow_id="build", confidence=0.2, router_version="v1",
        verdict="BLOCK", checks_passed=0, checks_total=2,
        fix_directive="api_key=sk-ABCDEF123456 leaked",
    ))
    learner = Learner(store)
    cands = learner.learn()
    assert len(cands) == 1
    desc = cands[0].description
    assert "sk-ABCDEF123456" not in desc
    assert "api_key=***" in desc or "api_key" in desc

# --------------------------------------------------------------- T36-F6
def test_t36_f6_rate_limiter_ignores_xff_without_trust(monkeypatch):
    """Outside Cloud Run / NINE_TRUST_PROXY, a client-supplied
    X-Forwarded-For must NOT key the bucket — rotating XFF must not
    bypass the limiter, and the socket peer must be the key."""

    from deploy.server import _check_rate_limit, _hits

    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("NINE_TRUST_PROXY", raising=False)
    _hits.clear()
    # 60 requests from the SAME socket peer with rotating XFF tails
    for i in range(30):
        req = types.SimpleNamespace(
            headers={"x-forwarded-for": f"9.9.9.{i % 250}, 9.9.9.{i % 250 + 1}"},
            client=types.SimpleNamespace(host="10.0.0.1"),
        )
        assert _check_rate_limit(req) is None
    # 31st from the same peer is throttled (rotating XFF did not split)
    req31 = types.SimpleNamespace(
        headers={"x-forwarded-for": "9.9.9.99, 9.9.9.100"},
        client=types.SimpleNamespace(host="10.0.0.1"),
    )
    assert _check_rate_limit(req31) is not None
    # a different peer gets its own bucket
    req_other = types.SimpleNamespace(
        headers={"x-forwarded-for": "9.9.9.1, 9.9.9.2"},
        client=types.SimpleNamespace(host="10.0.0.2"),
    )
    assert _check_rate_limit(req_other) is None
    _hits.clear()

def test_t36_f6_cors_default_refuses_and_allowlist_serves(tmp_path, monkeypatch):
    """No CORS headers by default (same-origin only); NINE_CORS_ORIGINS
    allowlist serves Access-Control-Allow-Origin for listed origins."""
    import importlib

    monkeypatch.setenv("NINE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NINE_CORS_ORIGINS", "https://dash.example.com")
    import deploy.server as server_mod
    importlib.reload(server_mod)
    from starlette.testclient import TestClient

    client = TestClient(server_mod.app)
    r = client.options("/v1/stats", headers={
        "Origin": "https://dash.example.com",
        "Access-Control-Request-Method": "GET",
    })
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "https://dash.example.com"
    # a disallowed origin gets no CORS blessing
    r2 = client.options("/v1/stats", headers={
        "Origin": "https://evil.example.com",
        "Access-Control-Request-Method": "GET",
    })
    assert r2.headers.get("access-control-allow-origin") is None

# --------------------------------------------------------------- T36-F7
def test_t36_f7_derive_keyword_skips_non_ascii():
    """_derive_keyword must never emit a mangled token from a non-ASCII
    task — déployer/日本語 yield "" (candidate says human-chosen)."""
    class _E:
        def __init__(self, t, w="research"):
            self.task_redacted = t
            self.workflow_id = w

    assert _derive_keyword(_E("déployer café ☕")) == ""
    assert _derive_keyword(_E("日本語のタスクです")) == ""
    # ASCII tasks still produce the longest informative token
    assert _derive_keyword(_E(
        "please investigate the quantum chromodynamics structure")) == "chromodynamics"

# --------------------------------------------------------------- T36-F8
def test_t36_f8_dedupe_is_case_whitespace_insensitive(tmp_path):
    """has() must catch candidates whose description differs only by
    case/whitespace (rescan after apply->revert noise)."""
    store = CandidateStore(tmp_path / "candidates.jsonl")
    c1 = ImprovementCandidate(
        candidate_id="cand-a", kind="gate",
        description="workflow 'build' BLOCKed with fix_directive 'fix A'; "
                    "consider a stricter gate or a recovery hop",
        evidence=["ev-1"],
    )
    store.append(c1)
    # drifted twin: case + extra whitespace
    assert store.has(
        "Workflow  'build'  blocked with fix_directive 'fix A'; "
        "CONSIDER a stricter gate or a recovery hop",
        ["ev-1"],
    ) is True
    # genuinely different description still allowed
    assert store.has("a completely different description", ["ev-1"]) is False

# --------------------------------------------------------------- coerce
def test_coerce_candidate_rejects_wrong_types():
    assert _coerce_candidate({"candidate_id": "x", "kind": 1}) is None
    assert _coerce_candidate({"candidate_id": "x", "kind": "k",
                              "description": "d", "evidence": "notalist"}) is None
    assert _coerce_candidate({"candidate_id": "x", "kind": "k",
                              "description": "d", "evidence": ["ev-1"],
                              "params": {"a": 1}}) is not None


# ------------------------------------------------------ F1 wiring armor
def test_t36_f1_no_output_runtimeerror_never_embeds_raw_task(tmp_path):
    """WIRING armor: the actual 'produced no output' RuntimeError raised by
    ADKAgentNode.__call__ must carry the redacted fragment — a revert of the
    task[:120] -> _safe_task_fragment wiring would re-leak credentials into
    CLI stderr / HTTP 502 even if the helper itself stays correct."""
    from types import SimpleNamespace

    from nine.runtime.adk_runtime import ADKAgentNode

    async def _create_session(**kw):
        return None

    node = object.__new__(ADKAgentNode)
    node._attempt_seq = 0
    node.agent = None
    node.app_name = "nine"
    node.runner = SimpleNamespace(
        run=lambda **kw: iter([]),  # fully empty stream
        session_service=SimpleNamespace(create_session=_create_session),
    )
    node._created_sessions = set()

    leaky_task = "change the password to hunter2 and use AIzaSyD1234567890abcdef"
    with pytest.raises(RuntimeError) as ei:
        node({"task": leaky_task}, tmp_path)
    msg = str(ei.value)
    assert "hunter2" not in msg
    assert "AIzaSyD1234567890abcdef" not in msg
    assert "<task redacted>" in msg or "redacted" in msg

def test_t36_f1_max_llm_calls_runtimeerror_never_embeds_raw_task(tmp_path):
    """WIRING armor: the max_llm_calls RuntimeError must also route through
    _safe_task_fragment (LlmCallsLimitExceededError injected via fake
    runner — the small-model-loop path)."""
    from types import SimpleNamespace

    from google.adk.agents.invocation_context import LlmCallsLimitExceededError

    from nine.runtime.adk_runtime import ADKAgentNode

    async def _create_session(**kw):
        return None

    def _boom(**kw):
        raise LlmCallsLimitExceededError("budget")

    node = object.__new__(ADKAgentNode)
    node._attempt_seq = 0
    node.agent = None
    node.app_name = "nine"
    node.runner = SimpleNamespace(
        run=_boom,
        session_service=SimpleNamespace(create_session=_create_session),
    )
    node._created_sessions = set()

    leaky_task = "deploy the secret ghp_ABCDEF1234567890 to prod"
    with pytest.raises(RuntimeError) as ei:
        node({"task": leaky_task}, tmp_path)
    msg = str(ei.value)
    assert "ghp_ABCDEF1234567890" not in msg
    assert "<task redacted>" in msg or "redacted" in msg
