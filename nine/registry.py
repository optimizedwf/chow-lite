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
import os
import sys
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
from nine.gates.evidence import EvidenceGate
from nine.runtime.responder import respond_gate, respond_workflow
from nine.runtime.workflows import Workflow
from nine.workflows.analyze_wf import analyze_hop
from nine.workflows.build_multi_wf import build_multi_hop
from nine.workflows.compare_wf import compare_hop
from nine.workflows.compose_wf import compose_hop
from nine.workflows.debug_wf import debug_hop
from nine.workflows.deploy_check_wf import deploy_check_hop
from nine.workflows.document_wf import document_hop
from nine.workflows.draft_email_wf import draft_email_hop
from nine.workflows.draft_wf import draft_hop
from nine.workflows.extract_wf import extract_hop
from nine.workflows.ideate_wf import ideate_hop
from nine.workflows.pipeline_wf import pipeline_hop
from nine.workflows.refactor_wf import refactor_hop
from nine.workflows.research_deep_wf import research_deep_hop
from nine.workflows.research_quick_wf import research_quick_hop
from nine.workflows.review_multi_wf import review_multi_hop
from nine.workflows.summarize_standalone_wf import summarize_standalone_hop
from nine.workflows.test_wf import test_hop
from nine.workflows.transform_wf import transform_hop

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
    except (json.JSONDecodeError, OSError) as e:
        # torture T4-F3: a corrupt/truncated catalog (bad manual edit, crash
        # mid-learn-apply) must NOT brick every `nine` command at import
        # time. Degrade to the base keyword set and say so loudly.
        print(
            f"warning: router catalog {_CATALOG_PATH} unreadable ({e}); "
            "using base keywords",
            file=sys.stderr,
        )
        return {"keyword_overrides": {}, "description_overrides": {}}
    if not isinstance(data, dict):
        print(
            f"warning: router catalog {_CATALOG_PATH} is not a JSON object; "
            "using base keywords",
            file=sys.stderr,
        )
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
    "transform": _wf(transform_hop),
    "pipeline": _wf(pipeline_hop),
    "analyze": _wf(analyze_hop),
    "compare": _wf(compare_hop),
    "compose": _wf(compose_hop),
    "debug": _wf(debug_hop),
    "draft": _wf(draft_hop),
    "draft-email": _wf(draft_email_hop),
    "deploy-check": _wf(deploy_check_hop),
    "document": _wf(document_hop),
    "extract": _wf(extract_hop),
    "ideate": _wf(ideate_hop),
    "research-quick": _wf(research_quick_hop),
    "research-deep": _wf(research_deep_hop),
    "summarize-standalone": _wf(summarize_standalone_hop),
    "refactor": _wf(refactor_hop),
    "teach": _wf(teach_hop),
    "respond": respond_workflow,
}

# hop factories per single-hop workflow id (used to build per-hop gates)
_HOPS: dict[str, Callable] = {
    "research": research_hop,
    "plan": lambda: plan_hop(require_handoff=False),
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
    # T20-F1 (slice 37): the router can select these 11 lanes, but they were
    # missing from _HOPS, so workflow_gate() returned None -> the CLI fell
    # back to the generic eval-json/exit-codes gate, which lanes that never
    # write EVAL.json (analyze/compare/draft/draft-email/extract/ideate/
    # summarize-standalone) can NEVER satisfy -> those jobs FIX-looped 2
    # extra full DAG runs and then BLOCKed. Every id in WORKFLOWS must have
    # a hop gate so submit/recover certify the lane's OWN artifacts.
    "transform": transform_hop,
    "pipeline": pipeline_hop,
    "analyze": analyze_hop,
    "compare": compare_hop,
    "compose": compose_hop,
    "draft": draft_hop,
    "draft-email": draft_email_hop,
    "extract": extract_hop,
    "ideate": ideate_hop,
    "research-deep": research_deep_hop,
    "summarize-standalone": summarize_standalone_hop,
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


def default_gate() -> EvidenceGate:
    """Generic fallback gate (eval-json + exit-codes only).

    Only correct for lanes whose nodes write EVAL.json from their ACTUAL
    run; hop gates (workflow_gate) certify per-lane artifacts. This exists
    so `resolve_gate` never returns None and every submit/recover/API path
    has a deterministic verdict.
    """
    from nine.gates.evidence import EvidenceGate, eval_json_check, exit_codes_check

    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    gate.register_check("exit-codes", exit_codes_check())
    return gate


def resolve_gate(workflow_id: str) -> EvidenceGate:
    """Per-hop gate for the workflow, falling back to the generic gate.

    THE single dispatch both the CLI (`nine submit`/`nine recover`) and the
    HTTP API use (T20-F2, slice 37): one expression, one verdict per
    evidence set. Prior to slice 37 the CLI and deploy/server.py each had
    their own `workflow_gate(...) or <local generic>` — a drift risk the
    torture-20 worker demonstrated by reading the server's dead
    `gate = build_gate()` assignment as the live verdict path.
    """
    return workflow_gate(workflow_id) or default_gate()

def _load_plugin_workflows() -> dict[str, Callable[[], Workflow]]:
    """Compose-registered plugin workflows (nine/chains/plugins/).

    The compose meta-workflow writes generated workflow modules into
    nine/chains/plugins/ and appends their factories to
    plugin_registry.py; this merge makes them executable through the
    same WORKFLOWS lookup as hand-written lanes. NINE_PLUGIN_REGISTRY
    overrides the registry file path (used by compose's own tests).
    """
    import importlib.util as _ilu

    default = (Path(__file__).resolve().parent / "chains" / "plugins"
               / "plugin_registry.py")
    reg_path = Path(os.environ.get("NINE_PLUGIN_REGISTRY") or default)
    if not reg_path.exists():
        return {}
    spec = _ilu.spec_from_file_location("_nine_plugin_registry", reg_path)
    if spec is None or spec.loader is None:
        return {}
    mod = _ilu.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001 - a broken plugin registry must not break nine
        # torture-12 F7: a syntax error / import failure silently disabled
        # EVERY plugin lane while operators believed composed lanes were
        # live (and learned keywords kept routing into dead ids). Warn
        # loudly, exactly like load_catalog does for corrupt catalogs.
        print(f"warning: plugin registry {reg_path} failed to load: {exc} - "
              "plugin workflows will NOT be available", file=sys.stderr)
        return {}
    return {
        wid: _wf(fac)
        for wid, fac in dict(getattr(mod, "PLUGIN_WORKFLOWS", {})).items()
    }


_PLUGIN_WORKFLOWS = _load_plugin_workflows()


CHAINS: dict[str, Callable[[], Chain]] = {
    "research-plan-build-review-teach": research_plan_build_review_teach,
    "inbox-triage-task-report": demo_lane,
}

# torture-14 F1 (T5-F2 regression): the CANNED demo lane must never be
# reachable from production routing. T5-F2 removed its keywords from
# _BASE_KEYWORDS, but the LEARN catalog merge path re-added them (the id IS
# in CHAINS, so the torture-12 F6 dead-id filter kept them) — a catalog
# override for "inbox-triage-task-report" routed real submits ("customer
# wants a refund") into the demo chain, which SHIPs hardcoded boilerplate
# as verified. Non-routable ids are dropped from the merged keywords with a
# loud warning and refused by `nine learn apply`.
NON_ROUTABLE_IDS: frozenset[str] = frozenset({"inbox-triage-task-report"})


def _merge_plugins() -> None:
    """Merge compose-registered plugin workflows with collision protection.

    torture-10 F4: WORKFLOWS.update() used to silently REPLACE core ids — a
    plugin named "research" (stale/hand-edited registry) hijacked every
    "research" submit with no warning. A plugin id colliding with a core
    workflow or chain id is SKIPPED with a loud warning: plugins are only
    ever user-generated lanes, never core replacements.
    """
    for pid in sorted(_PLUGIN_WORKFLOWS):
        if pid in WORKFLOWS or pid in CHAINS:
            print(
                f"warning: plugin workflow id {pid!r} collides with a core "
                "workflow/chain — SKIPPED (plugin lanes can never replace "
                "core lanes; torture-10 F4)",
                file=sys.stderr,
            )
            continue
        WORKFLOWS[pid] = _PLUGIN_WORKFLOWS[pid]


_merge_plugins()

# keywords used by the router registries (kept here so server/cli/demo agree)
_BASE_KEYWORDS: dict[str, list[str]] = {
    "research": ["research", "investigate", "find out", "study"],
    "plan": ["plan", "break down", "decompose", "roadmap", "step by step"],
    "build": ["build", "implement", "write code", "create the app",
            "create the service", "create the api", "create the cli",
            "create the function", "create the module", "create the script",
            "create the package"],
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
    "summarize-standalone": ["summarize", "summary", "summarize this", "tl;dr", "give me a summary", "summarize the code", "condense", "brief me", "short version", "executive summary"],
    "extract": ["extract", "extract data", "parse this", "convert to json", "convert this to json", "to json", "into json", "structured json", "extract the facts", "pull out the", "json output", "extract to json"],
    "compare": ["compare", "compare options", "which one is better", "compare the two", "compare these", "pros and cons", "compare and contrast", "which should i pick", "help me choose", "vs"],
    "draft": ["draft", "draft this", "write a draft", "write a proposal", "write an article", "write a spec", "first draft", "outline and draft", "write a plan"],
    "draft-email": ["draft an email", "write an email", "email reply", "compose an email", "outreach email", "follow up email", "email to", "reply to this email", "draft a response", "cold email"],
    "ideate": ["ideate", "brainstorm", "come up with ideas", "idea for", "give me ideas", "what should i build", "generate ideas", "think of an idea", "new product idea", "spark ideas"],
    "analyze": ["analyze", "analyze the data", "analyze this dataset", "explore the data", "data analysis", "what does the data show", "insights from", "analyze the csv", "explore the dataset", "look at the data"],
    "transform": ["transform", "transform this", "transform the data", "convert", "convert this", "convert this csv to json", "convert this json to csv", "convert this file to json", "convert this file to csv", "convert this file to yaml", "convert the csv", "convert the json", "convert file", "csv to json", "json to csv", "reformat", "reformat this", "change the format", "format conversion", "convert to yaml", "convert to tsv"],
    "pipeline": ["pipeline", "etl", "etl pipeline", "build a pipeline", "data pipeline", "run the pipeline", "process the data in stages", "transform and load", "multi-stage etl", "ingest and transform"],
    # torture-5 F2: the demo chain (inbox-triage-task-report) must NOT be
    # routable from production traffic - real user tasks ("customer wants a
    # refund") were SHIPping canned boilerplate as verified jobs. The chain
    # stays reachable ONLY via explicit `nine chain demo` / `chain
    # inbox-triage-task-report`, never through keyword routing.
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
    "summarize-standalone": "One-source distillation: read-source -> summarizer -> SUMMARY.md.",
    "extract": "Unstructured -> structured JSON: read-source -> extractor -> OUTPUT.json.",
    "compare": "Options vs criteria: criteria-extract -> analyzer -> comparator -> COMPARISON.md.",
    "draft": "Draft -> review -> revise: DRAFT.md + REVIEW.md + REVISION_LOG.md.",
    "draft-email": "Tone-aware email: draft -> reviewtone -> revise -> DRAFT.md.",
    "ideate": "Idea -> expand -> challenge -> refine: IDEA_BRIEF.md + VIABILITY.json.",
    "analyze": "Dataset -> explore -> insights: INSIGHTS.md + chart.png.",
    "transform": "Format conversion (CSV -> JSON etc.): detect -> transform -> validate: OUTPUT.EXT + EVAL.json.",
    "pipeline": "Multi-stage ETL (read -> transform -> load -> validate): OUTPUT.json + EVAL.json.",
    "inbox-triage-task-report": "Taskmaster lane: inbox -> triage -> task -> report.",
    "respond": "Direct answer to a general task (RESPONSE.md via Gemini).",
}


def _merged_keywords() -> dict[str, list[str]]:
    """Base keywords + git-tracked catalog overrides (LEARN-approved).

    torture-12 F6: catalog overrides can point at ids that are NOT
    executable (a plugin removed after its keyword was learned) — the
    router must never emit a dead id (submit would raw-traceback a
    WorkflowError). Drop unknown ids with a loud warning.
    """
    merged = {wf: list(kws) for wf, kws in _BASE_KEYWORDS.items()}
    overrides = load_catalog().get("keyword_overrides", {})
    if not isinstance(overrides, dict):
        # torture-6 F6: a valid-JSON-but-wrong-shape catalog (e.g.
        # keyword_overrides is a list) must degrade, not brick routing.
        print("warning: catalog keyword_overrides is not an object; ignored",
              file=sys.stderr)
        overrides = {}
    for wf, extra in overrides.items():
        if not isinstance(extra, (list, tuple)) or not all(
                isinstance(kw, str) for kw in extra):
            print(f"warning: catalog keyword_overrides[{wf!r}] is not a "
                  "list of strings; ignored", file=sys.stderr)
            continue
        seen = set(merged.get(wf, []))
        merged.setdefault(wf, [])
        for kw in extra:
            if kw not in seen:
                merged[wf].append(kw)
                seen.add(kw)
    # torture-12 F6: keywords for ids that cannot execute are dead routes —
    # drop them (warn once per id) so the router never emits them.
    executable = set(WORKFLOWS) | set(CHAINS)
    dead = [wf for wf in merged if wf not in executable]
    for wf in dead:
        print(f"warning: keyword entries for unregistered workflow id "
              f"'{wf}' dropped (removed plugin?); update the catalog or "
              "'nine learn' data", file=sys.stderr)
        del merged[wf]
    # torture-14 F1: keywords for NON-ROUTABLE ids (the canned demo lane)
    # are never allowed in — even a catalog/LEARN-approved override would
    # expose production traffic to the demo's hardcoded SHIPs.
    # torture-15 F8: normalize BOTH sides — a catalog override whose key is
    # a case/whitespace variant of a non-routable id (" Inbox-Triage-… ")
    # must still be dropped (the demo lane is never reachable from the
    # router, whatever the casing in the override file).
    blocked = {i.strip().casefold() for i in NON_ROUTABLE_IDS}
    for wf in list(merged):
        if wf.strip().casefold() in blocked:
            print(f"warning: keyword entries for non-routable workflow id "
                  f"'{wf}' dropped (demo lane is never reachable from the "
                  "router); remove it from nine/router/catalog.json "
                  "keyword_overrides", file=sys.stderr)
            del merged[wf]
    return merged


def _merged_descriptions() -> dict[str, str]:
    merged = dict(_BASE_DESCRIPTIONS)
    overrides = load_catalog().get("description_overrides", {})
    if not isinstance(overrides, dict):
        # torture-6 F6: same shape guard for descriptions.
        print("warning: catalog description_overrides is not an object; "
              "ignored", file=sys.stderr)
        overrides = {}
    for wf, desc in overrides.items():
        if isinstance(desc, str):
            merged[wf] = desc
        else:
            print(f"warning: catalog description_overrides[{wf!r}] is not a "
                  "string; ignored", file=sys.stderr)
    return merged


KEYWORDS: dict[str, list[str]] = _merged_keywords()
HOP_DESCRIPTIONS: dict[str, str] = _merged_descriptions()
