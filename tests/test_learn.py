"""LEARN loop tests — route events + candidate-only improvement suggestions."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chowlite.chains.chain import ChainExecutor
from chowlite.chains.flagship import demo_lane
from chowlite.learn.learner import Learner, RouteEvent, RouteEventStore
from chowlite.ledger.ledger import JSONLLedger


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
    from chowlite.registry import HOP_DESCRIPTIONS, KEYWORDS
    from chowlite.router.classifier import Router

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    store = RouteEventStore(tmp_path / "events.jsonl")
    learner = Learner(store)

    r = Router()
    for wf_id, kws in KEYWORDS.items():
        r.register(wf_id, kws, HOP_DESCRIPTIONS.get(wf_id, ""))
    decision = r.classify("trip to paris needs a plan")

    cex = ChainExecutor(ledger, workdir=tmp_path / "work", learner=learner)
    job = ledger.submit("inbox-triage-task-report", {"task": "trip to paris"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("trip to paris\n")
    (job_dir / "inbox.txt").write_text("trip to paris\n")
    res = cex.execute(demo_lane(), job, {"task": "trip to paris"}, decision=decision)
    assert res["final"] == "SHIPPED"

    events = store.all()
    assert events, "chain should record route events"
    ev = events[0]
    assert ev.confidence == decision.confidence, "real confidence, not 0.5"
    assert ev.router_version == decision.router_version, "real router version, not chain-v1"
    assert job.route_decision is not None
