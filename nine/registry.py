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
    plan_hop,
    research_hop,
    research_plan_build_review_teach,
    review_hop,
    teach_hop,
)
from nine.runtime.responder import respond_gate, respond_workflow
from nine.runtime.workflows import Workflow
from nine.workflows.build_multi_wf import build_multi_hop
from nine.workflows.debug_wf import debug_hop
from nine.workflows.deploy_check_wf import deploy_check_hop
from nine.workflows.document_wf import document_hop
from nine.workflows.refactor_wf import refactor_hop
from nine.workflows.research_quick_wf import research_quick_hop
from nine.workflows.review_multi_wf import review_multi_hop
from nine.workflows.test_wf import test_hop

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
    "plan": _wf(plan_hop),
    "build": _wf(build_hop),
    "review": _wf(review_hop),
    "review-multi": _wf(review_multi_hop),
    "test": _wf(test_hop),
    "build-multi": _wf(build_multi_hop),
    "debug": _wf(debug_hop),
    "deploy-check": _wf(deploy_check_hop),
    "document": _wf(document_hop),
    "research-quick": _wf(research_quick_hop),
    "refactor": _wf(refactor_hop),
    "teach": _wf(teach_hop),
    "respond": respond_workflow,
}

# hop factories per single-hop workflow id (used to build per-hop gates)
_HOPS: dict[str, Callable] = {
    "research": research_hop,
    "plan": plan_hop,
    "build": build_hop,
    "review": review_hop,
    "review-multi": review_multi_hop,
    "test": test_hop,
    "build-multi": build_multi_hop,
    "debug": debug_hop,
    "deploy-check": deploy_check_hop,
    "document": document_hop,
    "research-quick": research_quick_hop,
    "refactor": refactor_hop,
    "teach": teach_hop,
}


def workflow_gate(workflow_id: str):
    """EvidenceGate for a single-hop workflow, from the HOP's own gate checks.

    The generic gate (eval-json + exit-codes) is only correct for hops whose
    nodes write EVAL.json from their ACTUAL run; document hops (research.md /
    review.md / TEACH.md) certify by artifact, and the build hop by
    independent self-test EVAL.json — each hop declares what counts. There is
    no "collect node": every workflow id the router can select has a real,
    model-gated workflow.
    """
    from nine.gates.evidence import EvidenceGate

    if workflow_id == "respond":
        return respond_gate()
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
    "plan": ["plan", "break down", "decompose", "roadmap", "step by step"],
    "build": ["build", "implement", "write code", "create the"],
    "build-multi": ["multi-file", "multifile", "multi file", "multiple files", "scaffold", "full project", "project scaffold"],
    "review": ["review", "audit", "check the code", "qa"],
    "review-multi": ["multi review", "comprehensive review", "code review",
                     "security review", "pr review", "review this pr",
                     "review this pull request",
                     "pull request review", "review the code",
                     "review my code", "deep review", "review pr"],
    "test": ["test", "write tests", "pytest", "unit test", "make tests"],
    "debug": ["debug", "fix bug", "fix the bug", "bug", "diagnose", "root cause", "broken", "patch", "not working", "error", "crash", "failing"],
    "refactor": ["refactor", "restructure", "reorganize", "clean up the code", "improve the structure", "split the module", "extract functions", "rename internals"],
    "document": ["document", "documentation", "docs", "readme", "docgen", "api doc", "api reference", "write the readme", "explain the codebase", "how do i use", "how to use"],
    "deploy-check": ["deploy", "deployment", "pre-deploy", "deploy check", "ready to ship", "production readiness", "ready for production", "release check", "go live", "launch check"],
    "research-quick": ["research", "quick research", "look into", "find out", "investigate briefly", "what does this code do", "research this", "5 minute research", "quick lookup"],
    "research-deep": ["deep research", "thorough research", "comprehensive research", "deep dive", "research in depth", "iterative research", "critique my research", "in-depth analysis"],
    "inbox-triage-task-report": ["trip", "plan", "refund", "customer", "inbox"],
    "respond": ["hello", "hi", "help", "what can you do"],
}

_BASE_DESCRIPTIONS: dict[str, str] = {
    "research": "Produce a findings document (research.md).",
    "plan": "Break a task into ordered steps (PLAN.md + HANDOFF.md).",
    "build": "Implement per PLAN.md; write solution.py + EVAL.json.",
    "build-multi": "Scaffold a multi-file project under solution/ (main.py + package + tests).",
    "review": "Review a build; produce review.md verdict.",
    "review-multi": "4-dimensional review (security/bugs/quality/arch) merged into REVIEW.md.",
    "test": "Write and run pytest tests (test_solution.py + EVAL.json).",
    "debug": "Root-cause a failure, write ROOT_CAUSE.md, patch, and verify.",
    "refactor": "Restructure code per REFACTOR_PLAN.md, show DIFF.md, apply, and verify behavior intact.",
    "document": "Docgen for a codebase: inventory -> README.md + API.md.",
    "deploy-check": "Pre-deploy readiness: env scan + validate + risk review -> DEPLOY_CHECK.md Decision.",
    "research-quick": "Single-source quick research: plan -> findings -> receipt -> FINDINGS.md.",
    "research-deep": "Iterative deep research: draft -> critique -> iterate -> synthesize -> FINDINGS.md.",
    "inbox-triage-task-report": "Taskmaster lane: inbox -> triage -> task -> report.",
    "respond": "Direct answer to a general task (RESPONSE.md via Gemini).",
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
