"""Single source of truth for the ROUTE -> execution catalog.

Previously each entry point (chow submit, deploy/server.py, demo_live.py)
carried its own hard-coded workflow registry, so the router's ROUTE decision
selected no real behavior and research/review/build all produced
byte-identical artifacts. Now the router decision dispatches HERE.

Two mappings:
    WORKFLOWS: workflow_id -> single-hop Workflow factory
               (used by `chow submit` / POST /v1/submit for one-shot runs)
    CHAINS:    chain_id -> Chain factory
               (multi-hop chains with per-hop evidence gates)
"""
from __future__ import annotations

from collections.abc import Callable

from chowlite.chains.chain import Chain
from chowlite.chains.flagship import (
    build_hop,
    demo_lane,
    research_hop,
    research_plan_build_review_teach,
    review_hop,
    teach_hop,
)
from chowlite.runtime.workflows import Workflow


def _wf(hop_factory: Callable) -> Callable[[], Workflow]:
    """Adapt a Hop factory into a single-hop Workflow factory."""
    return lambda: hop_factory().workflow


WORKFLOWS: dict[str, Callable[[], Workflow]] = {
    "research": _wf(research_hop),
    "build": _wf(build_hop),
    "review": _wf(review_hop),
    "teach": _wf(teach_hop),
}

CHAINS: dict[str, Callable[[], Chain]] = {
    "research-plan-build-review-teach": research_plan_build_review_teach,
    "inbox-triage-task-report": demo_lane,
}

# keywords used by the router registries (kept here so server/cli/demo agree)
KEYWORDS: dict[str, list[str]] = {
    "research": ["research", "investigate", "find out", "study"],
    "build": ["build", "implement", "write code", "create the"],
    "review": ["review", "audit", "check the code", "qa"],
    "inbox-triage-task-report": ["trip", "plan", "refund", "customer", "inbox"],
    "respond": ["hello", "hi", "help", "what can you do"],
}

HOP_DESCRIPTIONS: dict[str, str] = {
    "research": "Produce a findings document (research.md).",
    "build": "Implement per PLAN.md; write solution.py + EVAL.json.",
    "review": "Review a build; produce review.md verdict.",
    "inbox-triage-task-report": "Taskmaster lane: inbox -> triage -> task -> report.",
    "respond": "Direct answer; no execution run.",
}
