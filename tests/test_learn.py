"""LEARN loop tests — route events + candidate-only improvement suggestions."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

from nine.chains.chain import ChainExecutor
from nine.chains.flagship import demo_lane
from nine.learn.learner import CandidateStore, Learner, RouteEvent, RouteEventStore
from nine.ledger.ledger import JSONLLedger


@pytest.fixture(autouse=True)
def _isolated_catalog(tmp_path, monkeypatch):
    """Every test writes to its OWN catalog (the router catalog is
    git-tracked + shared; learn apply/revert touches it for real)."""
    monkeypatch.setattr("nine.registry._CATALOG_PATH", tmp_path / "catalog.json")


def test_route_event_store_roundtrip(tmp_path):
    store = RouteEventStore(tmp_path / "events.jsonl")
    store.record(RouteEvent(
        event_id="ev-1", job_id="j1", task_redacted="x",
        workflow_id="research", confidence=0.9, router_version="v1",
        verdict="SHIP", checks_passed=2, checks_total=2,
    ))
    events = store.all()
    assert len(events) == 1
    assert events[0].workflow_id == "research"
    assert events[0].verdict == "SHIP"


def test_learner_proposes_candidates_never_autoapplies(tmp_path):
    store = RouteEventStore(tmp_path / "events.jsonl")
    store.record(RouteEvent(
        event_id="ev-b", job_id="j2", task_redacted="y",
        workflow_id="build", confidence=0.5, router_version="v1",
        verdict="BLOCK", checks_passed=0, checks_total=2,
        fix_directive="missing EVAL.json",
    ))
    learner = Learner(store)
    cands = learner.learn()
    assert len(cands) == 1
    assert cands[0].kind == "gate"
    assert cands[0].status == "pending"  # never auto-applied
    assert "EVAL.json" in cands[0].description


def test_chain_records_route_events(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    store = RouteEventStore(tmp_path / "events.jsonl")
    learner = Learner(store)
    ex = ChainExecutor(ledger, workdir=tmp_path / "work", learner=learner)

    job = ledger.submit("inbox-triage-task-report", {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "inbox.txt").write_text("hello\n")

    ex.execute(demo_lane(), job, {"task": "t"})
    events = store.all()
    assert len(events) >= 3  # one per hop
    assert all(e.verdict == "SHIP" for e in events)


def test_learner_candidates_are_durable_and_idempotent(tmp_path):
    """P1-5: candidates persist on disk; learn() never duplicates (per-event
    idempotency — a fresh Learner instance sees the same queue)."""
    store = RouteEventStore(tmp_path / "events.jsonl")
    store.record(RouteEvent(
        event_id="ev-c", job_id="j3", task_redacted="z",
        workflow_id="review", confidence=0.8, router_version="v1",
        verdict="FIX", checks_passed=1, checks_total=2,
    ))
    l1 = Learner(store)
    cands1 = l1.learn()
    assert len(cands1) == 1
    # a fresh learner (new process = new object) still sees the candidate
    l2 = Learner(store)
    assert len(l2.learn()) == 1          # no duplicate from re-scan
    l2.learn()                           # scan again -> still no dupes
    l3 = Learner(store)
    assert len(l3.cands.all()) == 1
    assert l3.cands.path.exists()        # durable on disk, not RAM-only


def test_chain_events_carry_real_route_decision(tmp_path):
    """P1-5: chain route events use the REAL confidence/router_version from
    the ROUTE step (previously hardcoded 0.5 / 'chain-v1')."""
    from nine.registry import HOP_DESCRIPTIONS, KEYWORDS
    from nine.router.classifier import Router

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    store = RouteEventStore(tmp_path / "events.jsonl")
    learner = Learner(store)

    r = Router()
    for wf_id, kws in KEYWORDS.items():
        r.register(wf_id, kws, HOP_DESCRIPTIONS.get(wf_id, ""))
    # demo keywords are NOT routable anymore (torture-5 F2); use a real
    # workflow's decision to prove route events carry the REAL confidence
    decision = r.classify("study black holes")

    cex = ChainExecutor(ledger, workdir=tmp_path / "work", learner=learner)
    job = ledger.submit("inbox-triage-task-report", {"task": "inbox item"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("inbox item\n")
    (job_dir / "inbox.txt").write_text("inbox item\n")
    res = cex.execute(demo_lane(), job, {"task": "inbox item"}, decision=decision)
    assert res["final"] == "SHIPPED"

    events = store.all()
    assert events, "chain should record route events"
    ev = events[0]
    assert ev.confidence == decision.confidence, "real confidence, not 0.5"
    assert ev.router_version == decision.router_version, "real router version, not chain-v1"
    assert job.route_decision is not None

# ---------------------------------------------------------------- P2

def _low_conf_event(store, task="study quantum chromodynamics", conf=0.18,
                    wf="research", ev_id="ev-low"):
    store.record(RouteEvent(
        event_id=ev_id, job_id="j-low", task_redacted=task,
        workflow_id=wf, confidence=conf, router_version="0.1.0",
        verdict="SHIP", checks_passed=1, checks_total=1,
    ))


def test_keyword_candidate_from_low_confidence_route(tmp_path):
    """P2: a low-confidence route to a KNOWN workflow proposes adding the
    strongest unmatched task token as a router keyword (machine-applicable).
    Uses a distinctive token so the assertion survives a catalog that already
    routes 'chromodynamics' (the LEARN apply gate re-runs this suite)."""
    store = RouteEventStore(tmp_path / "events.jsonl")
    _low_conf_event(store, task="study fooquark dynamics")
    cands = Learner(store).learn()
    assert len(cands) == 1
    c = cands[0]
    assert c.kind == "keyword"
    assert c.params["workflow_id"] == "research"
    assert c.params["keyword"] == "fooquark"
    assert c.params["task_hint"] == "study fooquark dynamics"


def test_unregistered_lane_candidate_is_human_owned(tmp_path):
    """P2: a low-confidence route to an UNREGISTERED workflow id has no
    keyword (respond is registered now — the universal lane), so apply
    refuses (a human must pick the workflow/keyword)."""
    store = RouteEventStore(tmp_path / "events.jsonl")
    _low_conf_event(store, task="zzz qqq something", conf=0.0,
                    wf="unregistered-lane", ev_id="ev-fb")
    cands = Learner(store).learn()
    assert len(cands) == 1
    c = cands[0]
    assert c.kind == "keyword"
    assert c.params["workflow_id"] == ""
    assert c.params["keyword"] == ""


def test_event_seeds_at_most_one_candidate_ever(tmp_path):
    """P2: once an event has a candidate (even after apply), a re-scan never
    re-suggests the same event (would otherwise propose the next-best keyword
    forever)."""
    store = RouteEventStore(tmp_path / "events.jsonl")
    _low_conf_event(store)
    learner = Learner(store)
    cands = learner.learn()
    assert len(cands) == 1
    # simulate apply: status transitions to applied; catalog now contains the
    # keyword, so a naive re-scan would derive a DIFFERENT keyword
    learner.cands.update_status(cands[0].candidate_id, "applied")
    rescan = Learner(store).learn()
    # the SAME candidate comes back; no NEW suggestion for the event
    assert [c.candidate_id for c in rescan] == [cands[0].candidate_id]
    assert rescan[0].status == "applied"


def test_candidate_store_status_roundtrip(tmp_path):
    store = RouteEventStore(tmp_path / "events.jsonl")
    _low_conf_event(store)
    learner = Learner(store)
    cid = learner.learn()[0].candidate_id
    learner.cands.update_status(cid, "applied")
    assert learner.cands.get(cid).status == "applied"
    learner.cands.update_status(cid, "pending")
    assert learner.cands.get(cid).status == "pending"


def test_wrong_shape_event_lines_are_skipped_not_crashed(tmp_path):
    """slice-52 (torture-34 F1): a valid-JSON WRONG-SHAPE route-event line
    (confidence as a string, verdict as a bool, checks_passed as a bool)
    used to construct a RouteEvent and then raw-crash `nine learn events`
    (format-code ValueError on ``{ev.confidence:.2f}``) and `nine learn scan`
    (str-vs-float TypeError). The read path must skip such lines — the same
    belt the ledger read path (T6-F3) gives corrupt lines."""
    import json

    store = RouteEventStore(tmp_path / "events.jsonl")
    bad_lines = [
        {"event_id": "ev-x-0", "job_id": "j", "task_redacted": "t",
         "workflow_id": "debug", "confidence": "0.99", "router_version": "v",
         "verdict": "SHIP", "checks_passed": 1, "checks_total": 1,
         "fix_directive": "", "recorded_at": "2026-08-16T00:00:00+00:00"},
        {"event_id": "ev-x-1", "job_id": "j", "task_redacted": "t",
         "workflow_id": "debug", "confidence": 0.8, "router_version": "v",
         "verdict": True, "checks_passed": 1, "checks_total": 1,
         "fix_directive": "", "recorded_at": "2026-08-16T00:00:00+00:00"},
        {"event_id": "ev-x-2", "job_id": "j", "task_redacted": "t",
         "workflow_id": "debug", "confidence": 0.8, "router_version": "v",
         "verdict": "FIX", "checks_passed": True, "checks_total": 1,
         "fix_directive": "", "recorded_at": "2026-08-16T00:00:00+00:00"},
    ]
    good = {"event_id": "ev-good", "job_id": "j", "task_redacted": "t",
            "workflow_id": "debug", "confidence": 0.9, "router_version": "v",
            "verdict": "SHIP", "checks_passed": 1, "checks_total": 1,
            "fix_directive": "", "recorded_at": "2026-08-16T00:00:00+00:00"}
    with store.path.open("a") as f:
        for rec in bad_lines + [good]:
            f.write(json.dumps(rec) + "\n")
    events = store.all()
    assert [e.event_id for e in events] == ["ev-good"]
    # the display path must not crash on the surviving (well-typed) line
    assert f"{events[0].confidence:.2f}" == "0.90"
    # the scan path must not crash either
    cands = Learner(store).learn()
    assert isinstance(cands, list)


def test_candidate_update_status_read_oserror_is_clean(tmp_path):
    """slice-52 (torture-34 F2): `CandidateStore.update_status` (the rewrite
    used by `nine learn apply`/`revert`) read its JSONL OUTSIDE the OSError
    belt — a directory at candidates.jsonl or a chmod-000 store
    raw-tracebacked IsADirectoryError/PermissionError. The read must be
    best-effort like the write side: warn on stderr, never crash."""
    import json
    import os as _os

    store = RouteEventStore(tmp_path / "events.jsonl")
    _low_conf_event(store)
    learner = Learner(store)
    cid = learner.learn()[0].candidate_id
    # directory at the candidates path
    cand_dir = tmp_path / "cand-dir"
    cand_dir.mkdir()
    cstore = CandidateStore(cand_dir)
    cstore.update_status(cid, "approved")  # must warn, not raise
    # chmod-000 store (read denied)
    cand_ro = tmp_path / "candidates-ro.jsonl"
    cand_ro.write_text(json.dumps({
        "candidate_id": cid, "kind": "gate", "description": "x",
        "evidence": [], "status": "pending",
    }) + "\n")
    _os.chmod(cand_ro, 0)
    cstore2 = CandidateStore(cand_ro)
    cstore2.update_status(cid, "approved")  # must warn, not raise
    # healthy store still transitions
    learner.cands.update_status(cid, "applied")
    assert learner.cands.get(cid).status == "applied"
