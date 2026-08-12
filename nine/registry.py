"""Single source of truth for the ROUTE -> execution catalog.

Previously each entry point (nine submit, deploy/server.py, demo_live.py)
carried its own hard-coded workflow registry, so the router's ROUTE decision
selected no real behavior and research/review/build all produced
byte-identical artifacts. Now the router decision dispatches HERE.

Two mappings:
    WORKFLOWS: workflow_id -> single-hop Workflow factory
               (used by `nine submit` / POST /v1/submit for one-shot runs)
    CHAINS:    chain_id -> Chain factory
               (multi-hop chains with per-hop evidence gates)
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from nine.chains.chain import Chain
from nine.chains.flagship import (
    build_hop,
    demo_lane,
    research_hop,
    research_plan_build_review_teach,
    review_hop,
    teach_hop,
)
from nine.runtime.workflows import Workflow

_CATALOG_PATH = Path(__file__).resolve().parent / "router" / "catalog.json"


def load_catalog() -> dict:
    """Read the git-tracked router catalog (keyword/description overrides).

    `nine learn apply` appends approved keyword suggestions here; rollback
    is a git revert. The catalog is the ONLY file the LEARN loop may write —
    human-approvable, regression-gated changes live in git history.
    """
    try:
        data = json.loads(_CATALOG_PATH.read_text())
    except FileNotFoundError:
        return {"keyword_overrides": {}, "description_overrides": {}}
    return data


def save_catalog(data: dict) -> None:
    _CATALOG_PATH.write_text(json.dumps(data, indent=2) + "\n")


def _wf(hop_factory: Callable) -> Callable[[], Workflow]:
    """Adapt a Hop factory into a single-hop Workflow factory."""
    return lambda: hop_factory().workflow


WORKFLOWS: dict[str, Callable[[], Workflow]] = {
    "research": _wf(research_hop),
    "build": _wf(build_hop),
    "review": _wf(review_hop),
    "teach": _wf(teach_hop),
}

# hop factories per single-hop workflow id (used to build per-hop gates)
_HOPS: dict[str, Callable] = {
    "research": research_hop,
    "build": build_hop,
    "review": review_hop,
    "teach": teach_hop,
}


def workflow_gate(workflow_id: str):
    """EvidenceGate for a single-hop workflow, from the HOP's own gate checks.

    The generic gate (eval-json + exit-codes) is only correct for the
    fallback collect node, which writes EVAL.json itself; document hops
    (research.md / review.md / TEACH.md) certify by artifact, and the build
    hop by independent self-test EVAL.json — each hop declares what counts.
    """
    from nine.gates.evidence import EvidenceGate

    factory = _HOPS.get(workflow_id)
    if factory is None:
        return None
    gate = EvidenceGate()
    for name, check in factory().gate_checks.items():
        gate.register_check(name, check)
    return gate

CHAINS: dict[str, Callable[[], Chain]] = {
    "research-plan-build-review-teach": research_plan_build_review_teach,
    "inbox-triage-task-report": demo_lane,
}

# keywords used by the router registries (kept here so server/cli/demo agree)
_BASE_KEYWORDS: dict[str, list[str]] = {
    "research": ["research", "investigate", "find out", "study"],
    "build": ["build", "implement", "write code", "create the"],
    "review": ["review", "audit", "check the code", "qa"],
    "inbox-triage-task-report": ["trip", "plan", "refund", "customer", "inbox"],
    "respond": ["hello", "hi", "help", "what can you do"],
}

_BASE_DESCRIPTIONS: dict[str, str] = {
    "research": "Produce a findings document (research.md).",
    "build": "Implement per PLAN.md; write solution.py + EVAL.json.",
    "review": "Review a build; produce review.md verdict.",
    "inbox-triage-task-report": "Taskmaster lane: inbox -> triage -> task -> report.",
    "respond": "Direct answer; no execution run.",
}


def _merged_keywords() -> dict[str, list[str]]:
    """Base keywords + git-tracked catalog overrides (LEARN-approved)."""
    merged = {wf: list(kws) for wf, kws in _BASE_KEYWORDS.items()}
    for wf, extra in load_catalog().get("keyword_overrides", {}).items():
        seen = set(merged.get(wf, []))
        merged.setdefault(wf, [])
        for kw in extra:
            if kw not in seen:
                merged[wf].append(kw)
                seen.add(kw)
    return merged


def _merged_descriptions() -> dict[str, str]:
    merged = dict(_BASE_DESCRIPTIONS)
    merged.update(load_catalog().get("description_overrides", {}))
    return merged


KEYWORDS: dict[str, list[str]] = _merged_keywords()
HOP_DESCRIPTIONS: dict[str, str] = _merged_descriptions()
