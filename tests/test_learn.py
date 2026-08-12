"""LEARN loop tests — route events + candidate-only improvement suggestions."""
import sys
from pathlib import Path

import pytest

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
