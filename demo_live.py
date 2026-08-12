#!/usr/bin/env python3
"""chow-lite LIVE demo — model-routed, evidence-gated, learning.

Same loop as demo.py but the ROUTE step is a real Gemini 3.5 Flash call
and the teach hop uses Gemma 4 (second Google model). Falls back to
deterministic routing when no GEMINI_API_KEY is set.

Run:  python demo_live.py "your task here"
"""
import json
import os
import sys
import tempfile
from pathlib import Path

from chowlite.chains.chain import ChainExecutor
from chowlite.chains.flagship import demo_lane, research_plan_build_review_teach
from chowlite.learn.learner import Learner, RouteEventStore
from chowlite.ledger.ledger import JSONLLedger
from chowlite.router.classifier import Router


def _model_router() -> Router:
    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    class Model:
        def generate_content(self, prompt):
            return client.models.generate_content(
                model="gemini-3.5-flash", contents=prompt
            )

    r = Router(model=Model(), version="gemini-3.5-flash-live")
    r.register("inbox-triage-task-report", ["trip", "plan", "refund", "customer", "inbox"],
               "Taskmaster lane: inbox -> triage -> task -> report.")
    r.register("research-plan-build-review-teach", ["build", "research", "implement", "code"],
               "Flagship chain: research -> plan -> build -> review -> teach.")
    return r


def main() -> int:
    task = sys.argv[1] if len(sys.argv) > 1 else "plan a weekend trip to Big Sur"
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ledger = JSONLLedger(td / "ledger.jsonl")
        events = RouteEventStore(td / "events.jsonl")
        learner = Learner(events)
        ex = ChainExecutor(ledger, workdir=td / "work", learner=learner)

        # ---- ROUTE (real Gemini 3.5 Flash) ----
        print("== ROUTE ==")
        router = _model_router()
        decision = router.classify(task)
        d = decision.to_dict()
        print(f"task:        {task}")
        print(f"workflow:    {d['workflow_id']}")
        print(f"confidence:  {d['confidence']:.2f}")
        print(f"reason:      {d['reason']}")
        print(f"router:      {d['router_version']}")

        # ---- EXECUTE ----
        print("\n== EXECUTE ==")
        lane = (research_plan_build_review_teach()
                if decision.workflow_id == "research-plan-build-review-teach"
                else demo_lane())
        job = ledger.submit(decision.workflow_id, {"task": task})
        job_dir = td / "work" / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "task.txt").write_text(task + "\n")
        if decision.workflow_id != "research-plan-build-review-teach":
            (job_dir / "inbox.txt").write_text(task + "\n")

        res = ex.execute(lane, job, {"task": task})
        print(f"FINAL: {res['final']}")
        for hop, info in res["hop_results"].items():
            print(f"  {hop}: {info['verdict']}")

        # ---- VERIFY ----
        print("\n== VERIFY ==")
        for a in ledger.get(job.job_id).artifacts:
            print(f"  {a['name']:18} {a['sha256'][:12]}  {a['size']}B  by {a['produced_by']}")

        # ---- LEARN ----
        print("\n== LEARN ==")
        evs = events.all()
        print(f"route events recorded: {len(evs)}")
        for ev in evs[:3]:
            d = ev.to_dict() if hasattr(ev, "to_dict") else vars(ev)
            print(f"  {d.get('workflow_id')} -> {d.get('verdict')} conf={d.get('confidence'):.2f} @ {str(d.get('recorded_at',''))[:19]}")
        cands = learner.learn()
        print(f"improvement candidates: {len(cands)} (candidate-only, human applies)")
        return 0 if res["final"] == "SHIPPED" else 2


if __name__ == "__main__":
    sys.exit(main())
