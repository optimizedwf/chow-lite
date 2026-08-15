# NINE — The Complete Workflow Roadmap (Mini-AGI for Everyone)

> Born from 200+ Chow/Archon reference workflows, rebuilt lighter, smarter,
> and model-driven. Nine is the modern, public, evidence-gated agent OS.
> Every workflow follows the nine loop: ROUTE → EXECUTE → VERIFY → LEARN.

## Design Principles (better than Chow, not just lighter)

1. **Model-or-fail.** No fabricated outputs, ever (already enforced).
2. **Multi-tier per category.** Not one "research" WF — three:
   `research-quick`, `research`, `research-deep`. Match task complexity.
3. **Every WF produces evidence.** An artifact (not just a log) certifies
   completion. The builder never certifies itself.
4. **ADK-first.** Complex nodes are ADK LlmAgents with real tools (write_file,
   read_file, run_command). Not bash echo stubs.
5. **Composable.** Hops chain into multi-hop pipelines. Specialist hops
   become reusable building blocks.
6. **Learn-enabled.** Every Wf run feeds LEARN (route events + keyword
   catalog + failure patterns via MemoryGraph).
7. **The compose meta-wf.** A custom task with no matching wf gets a
   one-shot custom workflow **built on the fly** by the compose lane
   (spec → generate Python → gate → register). This is the AGI move.

## Architecture: Per-Node Type Reference

Nine has 5 node `kind` types; every new Wf uses the right one:

| kind | When | What it does |
|------|------|-------------|
| `prompt` | Pure text generation (no tools) | Gemini via google.genai → writes an .md artifact |
| `tool` | Use ADK LlmAgent with real tools | LlmAgent with write_file/read_file/run_command → real code/output |
| `bash` | Deterministic work (build, test, lint) | Shell command → side effects + EVAL.json |
| `subagent` | Subagent delegation | Spawned RLM child with brief subtask |
| `summarize` | Distillation | Summarize a prev node's artifact (built-in summarizer) |

Every prompt/tool node MUST import `WorkflowError` at module top and raise
it loudly on missing key/empty model output (model-or-fail doctrine).

## The Complete Catalog: 9 Lanes × Multiple WFs Each

### Lane A — Coding / Engineering (The Maker) [HIGHEST PRIORITY]
The most common agent task category. Many sub-uses.

| WF ID | Purpose | Nodes | Gate |
|-------|---------|-------|------|
| `build` ✅ | Implement code per plan; self-test ADK + bash | build (tool/ADK) → self-test (bash writes EVAL.json) | solution.py + EVAL.json passed-checks |
| `build-multi` ← NEW | Multi-file build scaffolded via ADK; multi-write | build (tool/ADK multi-write_file) → test (bash runs pytest) | solution/ + EVAL.json |
| `test` ← NEW | Write tests for a spec/module; run them | spec-reader (tool/ADK) → writer (tool/ADK writes *_test.py) → runner (bash runs pytest → EVAL.json) | test file + EVAL.json passed |
| `debug` ← NEW | Root-cause a failure → patch → verify | symptom (prompt model) → diagnose (tool/ADK reads logs) → patch (tool/ADK writes patch.py) → regen (bash runs regression) | ROOT_CAUSE.md + patched code + EVAL.json |
| `debug-multi` ← NEW | Multi-specialist debug (symptoms + root + regression-risk + historical) — parallel analyse → merge | 4 parallel prompt nodes → synthesizer prompt node → patcher tool → test bash | ROOT_CAUSE.md + patch + EVAL.json |
| `review` ✅ | Single-dimensional lightweight review | review (bash/prompt) → verdict | review.md + EVAL.json |
| `review-multi` ← NEW | Multi-dimensional review (security + bugs + quality + arch) — parallel analyse → merge | 4 parallel prompt nodes (security/bugs/quality/arch) → merger prompt | per-dim review files + final REVIEW.md |
| `refactor` ← NEW | Restructure + verify behavior intact | context-read (bash) → planner (tool/ADK edit-spec) → human-diff-gate (prompt prints before/after) → apply (tool/ADK applies) → verify (bash runs tests) | REFACTOR_RECEIPT.json |
| `document` ← NEW | Docgen for a codebase: README + API doc | inventory (bash) → docgen (tool/ADK reads + writes README.md + API.md) | README.md + API.md |
| `deploy-check` ← NEW | Pre-deploy readiness: env + validate + risk-review | preflight (bash) → env-scan (bash) → validate (bash runs tests) → risk (prompt model) → decision (prompt model) | DEPLOY_CHECK.md with Decision field |
| `spec-from-idea` ← NEW | Turn idea → implementation-ready spec | context (bash) → spec (tool/ADK writes SPEC.md) → acceptance (tool/ADK writes ACCEPTANCE.md) → tasks (tool/ADK writes TASKS.json) | SPEC.md + ACCEPTANCE.md + TASKS.json |
| `audit` ← NEW | Deterministic repo scan (deps, secrets, lint) | scan-deps (bash) → scan-secrets (bash) → lint (bash) → summarize (prompt model) | FINDINGS.json |
| `adversarial` ← NEW | GAN-inspired dev: planner→generator vs evaluator loop | plan (tool/ADK) → init-workspace (bash) → adversarial-sprint (tool/ADK gen vs eval loop) → report (prompt) | SPRINT_RESULTS.md + final code |
| `investigate` ← NEW | Reproduce + draft an issue report | classify (prompt) → git-context (bash) → dedup (bash) → investigate (tool/ADK) → reproduce (bash) → draft (tool/ADK) | INVESTIGATION.md + REPRO_STEPS.json |
| `resolve-conflicts` ← NEW | Rebases + auto-resolves merge conflicts | rebase (bash) → classify-conflicts (prompt) → resolve-simple (tool/ADK) → resolve-complex (tool/ADK) → report (prompt) | CONFLICT_REPORT.md |
| `validate` ← NEW | E2E PR validation: review→test→report | fetch-pr (bash) → resolve-paths (bash) → code-review (prompt) → classify-testability (prompt) → e2e-test (bash) → cleanup (bash) → report (prompt) | VALIDATION_REPORT.md + EVAL.json |

### Lane B — Research / Information (The Scholar)
| WF ID | Purpose | Nodes | Gate |
|-------|---------|-------|------|
| `research-quick` ← NEW | Single-source quick research (5 min) | search-prep (prompt model) → researcher (tool/ADK) → receipt | FINDINGS.md with sections |
| `research` ✅ | Multi-source synthesis | research (tool/ADK) → summarize (summarize) → HANDOFF.md | research.md + HANDOFF.md |
| `research-deep` ← NEW | Iterative deep research (multiple loops) | researcher (tool/ADK) → critique (prompt model) → iterate (tool/ADK) → synthesize (prompt) | FINDINGS.md + critique pass |
| `summarize` ← NEW standalone | One-source distillation | read-source (bash) → summarizer (summarize) | SUMMARY.md |
| `extract` ← NEW | Unstructured → structured JSON | read-source (bash) → extractor (tool/ADK with write_file writes OUTPUT.json) | OUTPUT.json (valid JSON) |
| `compare` ← NEW | 2+ options vs criteria → recommendation | criteria-extract (prompt model) → per-option-analyzer (tool/ADK) → comparator (prompt model) | COMPARISON.md with recommendation |
| `repo-scan` ← NEW | Codebase structural scan | structure (bash) → deps (bash) → summary (prompt model) | SCAN.md |
| `fact-check` ← NEW | Verify claims against knowledge/text | collect-claims (prompt) → per-claim-verifier (parallel prompt nodes) → consolidate | VERIFIED.md with verdicts |
| `review-adaptive` ← NEW | Smart review: classify complexity → run only relevant agents | scope (bash) → classify (prompt model) → [conditional parallel review agents] → synthesize (prompt) → implement-fixes (tool/ADK) | REVIEW.md + FIX_LOG.json |

### Lane C — Writing / Drafting (The Scribe)
| WF ID | Purpose | Nodes | Gate |
|-------|---------|-------|------|
| `draft` ← NEW | Spec/proposal/article with draft→review→revise loop | draft (tool/ADK) → review (prompt model) → revise (tool/ADK) | DRAFT.md + final + revision log |
| `draft-email` ← NEW | Compose reply/outreach with tone spec | draft (prompt model) → reviewtone (prompt model) → revise | DRAFT.md |
| `ideate` ← NEW | Take raw idea → expand → challenge → refine | expand (prompt) → challenge (prompt) → refine (prompt) | IDEA_BRIEF.md + VIABILITY.json |

### Lane D — Data / Analysis (The Analyst)
| WF ID | Purpose | Nodes | Gate |
|-------|---------|-------|------|
| `analyze` ← NEW | Dataset → explore → insights | inspect (bash: pandas) → explore (tool/ADK) → visualize (bash: matplotlib) → report (prompt) | INSIGHTS.md + chart.png |
| `transform` ← NEW | Format conversion (CSV→JSON, etc.) | detect-format (bash) → transform (tool/ADK with write_file) → validate (bash schema check) | OUTPUT.EXT + EVAL.json |
| `pipeline` ← NEW | Multi-stage ETL (read → transform → load → validate) | read (bash) → transform (tool/ADK) → load (bash) → validate (bash) | OUTPUT.json + EVAL.json |

### Lane E — Planning / Strategy (The Strategist)
| WF ID | Purpose | Nodes | Gate |
|-------|---------|-------|------|
| `plan` ← REGISTER (exists in flagship) | Decompose goal into ordered steps | plan (tool/ADK) | PLAN.md |
| `plan-deep` ← NEW | Deep planning with estimate + risk + ramification | plan (tool/ADK) → estimate (prompt) → risk (prompt) → roadmap (prompt) | PLAN.md + RISKS.md + ROADMAP.md |
| `goal-decompose` ← NEW | Vague directive → goal tree | decompose (tool/ADK) | GOAL_TREE.json |

### Lane F — Meta / Cognitive (The Workflow-Builder) [STANDOUT FEATURE]
This is what makes nine **mini-AGI**: not just hardcoded WFs but a meta-WF
that builds new WFs on demand. The compose lane is the differentiator.

| WF ID | Purpose | Nodes | Gate |
|-------|---------|-------|------|
| `critique` ← NEW | Stress-test any artifact (adversarial) | read (prompt) → attack (prompt) → score (prompt) | CRITIQUE.md + QUALITY_SCORE.json |
| `fix-loop` ✅ (engine-level) | Re-run with fix_directive on FIX verdict | Built into WorkflowExecutor.execute (already) | gate re-check |
| `self-improve` ← NEW | Harvest failures from gaps → propose wf patches | collect-gaps (bash) → propose (tool/ADK) → gate (prompt) → queue | REVIEW_QUEUE.json |
| `failure-analyze` ← NEW | Classify failures → update catalog → generalizations | classify (bash) → update-catalog (bash) → analyze (prompt) | FAILURE_REPORT.json |
| `compose` ← NEW THE META-WF | Build a custom Wf for a novel task | spec (tool/ADK writes SPEC.md) → structure-design (tool/ADK writes HOP_SPEC.json) → implement (tool/ADK writes Python code as string) → test (bash compiles + run pytest) → register (bash writes to nine/chains/plugins/) → validate (bash runs compose gate) | Wf SOURCE FILE + TEST FILE passed |
| `compose-spec` ← NEW | Just generate the wf spec without implement | spec (tool/ADK) → advisory-review (prompt) | SPEC.md |

### Lane G — Knowledge / Learning (The Teacher)
| WF ID | Purpose | Nodes | Gate |
|-------|---------|-------|------|
| `teach` ✅ | Lesson extraction from completed task | gemma (prompt/gemma_generate) → writes TEACH.md | TEACH.md nonempty |
| `onboard` ← NEW | Read a repo → onboarding guide | structure (bash) → docgen (tool/ADK writes ONBOARDING.md) → review (prompt) | ONBOARDING.md |
| `classify` ← NEW | Categorize item against a taxonomy | taxonomy-load (prompt) → classify (tool/ADK) | CLASSIFICATION.json |
| `glossary` ← NEW | Build a glossary from arbitrary text | extract (prompt) → consolidate (prompt) → verify (prompt) | GLOSSARY.md |

### Lane H — Ops / Automation (The Operator) [LATER PRIORITY]
| WF ID | Purpose | Nodes | Gate |
|-------|---------|-------|------|
| `triage` ← NEW | Classify + prioritize incoming items | classify (tool/ADK) → prioritize (prompt) → route (prompt) | TRIAGE.md |
| `monitor` ← NEW | Poll source → detect anomaly → alert | sample (bash) → detector (prompt) → alert-decide (prompt) | ALERT.md |
| `deploy` ← NEW | Build → release → health-check → report | preflight (bash) → build (tool/ADK or subagent) → deploy (bash) → healthcheck (bash) | REPORT.md + status |
| `content-factory` ← NEW | Programmatic SEO/geo page generation | scan-site (bash) → expand (tool/ADK) → validate (bash) → deploy (bash) | PAGES_MANIFEST.json |
| `image-factory` ← NEW | Grounded image prompt pack from brief | brief-read (prompt) → template-select (prompt) → generate-prompts (tool/ADK) → render (bash) | PROMPT_PACK.json |

### Lane I — Security / Proof (The Auditor)
| WF ID | Purpose | Nodes | Gate |
|-------|-------|-------|------|
| `security-audit` ← NEW | OWASP audit: deps + secrets + SAST → triage | dep-audit (bash) → secrets-scan (bash) → sast (bash) → triage (prompt) | SECURITY_REPORT.md + FINDINGS.json |
| `verify` ✅ | Audit a workspace against a task's claims | collect (bash) → claims (prompt) → check (bash deterministic) → verdict (prompt) | VERIFIED.json + CHECKS.json (honesty gate: no hidden FAILs, exact claim parity, verdict follows evidence) |
| `eval-hash` ← NEW | Aggregate-eval: run a gate harness → score | collect (bash) → run-gate (bash) → score (prompt) | EVAL-Agent Scorecard |

## Chain Catalog (Multi-hop pipelines)

Existing chains will compose new hops:

| Chain ID | Hops | Purpose |
|----------|------|---------|
| `research-plan-build-review-teach` ✅ | research → plan → build → review → teach | flagship |
| `inbox-triage-task-report` ✅ | triage → task → report | demo |
| `build-test-debug-review` ← NEW | build → test → debug → review | coding loop |
| `research-draft-review` ← NEW | research-draft → draft → critique→revise | writing |
| `spec-build-test` ← NEW | spec-from-idea → build → test | spec2ship |
| `idea-to-spec` ← NEW | ideate → plan → spec-from-idea | brainstorm-to-spec |

The user's custom task could land on a chain if the router detects a
multi-phase intent (via the compose lane or the goal-decompose chain).

## Build Order (Sequential, No Skipping)

Slices in priority order. Each is a full DoD: pytest + ruff + mypy clean →
one commit → push → Dell sync → memory entry.

1. **Register `plan` in WORKFLOWS** (one-line fix — already built in
   flagship). Add keywords for "plan", "break down".
2. **Lane A `test` workflow** — highest impact gap. ADK reads a module/spec
   and writes *_test.py. Bash runs pytest. Gate EVAL.json passed-checks.
3. **Lane A `debug` workflow** — symptom → diagnose → patch → regress.
4. **Lane A `build-multi`** — multi-file build (currently build is 1-file
   ADK). Add a new build variant for multi-file projects.
5. **Lane A `review-multi`** — four parallel reviewer personas merged.
6. **Lane A `refactor`** — codebase surgery with diff + verify intact.
7. **Lane A `document`** — docgen from codebase.
8. **Lane A `deploy-check`** — pre-deploy readiness check.
9. **Lane B `research-quick`** — lightweight single-source.
10. **Lane B `research-deep`** — iterative with critique.
11. **Lane B `summarize-standalone`** — promote existing summarizer node.
12. **Lane B `extract`** — unstructured → structured JSON.
13. **Lane B `compare`** — multi-option decision with criteria.
14. **Lane C `draft`** — draft→review→revise loop.
15. **Lane C `draft-email`** — tone-aware reply/compose.
16. **Lane C `ideate`** — expand→challenge→refine.
17. **Lane D `analyze`** — dataset exploration.
18. **Lane D `transform`** — format conversion.
19. **Lane D `pipeline`** — ETL.
20. **Lane E `plan-deep`** — multi-section deep planning.
21. **Lane E `goal-decompose`** — vague → goal tree.
22. **Lane F `critique`** — adversarial quality gate.
23. **Lane F `self-improve`** — gap→patch proposal with eval gate.
24. **Lane F `failure-analyze`** — classify + generalize.
25. **Lane F `compose`** — THE META-WF: spec→structure→implement→test→register. **PIVOTAL.**
26. **Lane F `compose-spec`** — advisory-only variant.
27. **Lane G `onboard`** — repo onboarding guide.
28. **Lane G `classify`** — categorize against taxonomy.
29. **Lane G `glossary`** — build glossary.
30. **Lane H `triage`** — inbox triage.
31. **Lane H `monitor`** — anomaly detection.
32. **Lane H `deploy`** — full deploy (needs live infra).
33. **Lane I `security-audit`** — OWASP audit pipeline.
34. **Lane I `verify`** — generic output verifier.
35. **Lane I `eval-hash`** — aggregate scoring.
36. **Lane A `adversarial`** — GAN dev (after core lanes).
37. **Lane A `investigate`** — issue reproduction.
38. **Lane A `resolve-conflicts`** — merge conflict resolver.
39. **Lane A `validate`** — e2e PR validation.
40. **Lane B `review-adaptive`** — adaptive complexity-based review.
41. **Lane H `content-factory`** — SEO pages (LATER).
42. **Lane H `image-factory`** — image prompt packs (LATER).

After lane I, plan chains that compose the new hops (build-test-debug-review,
spec-build-test, etc.).

## Architecture Improvements Needed to Support the New WFs

### A. New gate check: `text_contains_check`
Returns pass/fail by regex match on an artifact (used for "contains PASS"
reviews, security finding sections, etc.). Add to `nine/gates/evidence.py`.

### B. New gate check: `json_valid_check`
Parses a JSON file and checks key presence (for OUTPUT.json, FINDINGS.json,
etc.). Add to `nine/gates/evidence.py`.

### C. New gate check: `eval_json_passes_check(expected_passes=[...])`
Existing `eval_json_check` only validates existence; need a variant that
requires specific checks to pass (for test workflows where the EVAL.json
must show test-ran true).

### D. ADK tool catalog (reusable FunctionTools)
A common module for tools: `write_file(path, content)`, `read_file(path)`,
`read_text_file(path)`, `write_text_file`, `list_dir()`, `python_eval(code)`,
`shell(cmd)` — exported as a catalog so new ADK nodes compose reuse.
Put at `nine/runtime/adk_tools.py`.

### E. Plugins directory for compose-generated Wfs
`nine/chains/plugins/` — directory where compose writes generated Python
files. Plugin loader at import time so the registry can discover them.

### F. Chain composition helpers (lane-cross compose)
`nine/chains/composer.py` to build chains dynamically given a hop list.

### G. Async support for ADK (already works via the sync runner — verified)

## Next Concrete Step

Slice #1 is trivial — register existing `plan_hop` in WORKFLOWS. This is
already in flagship.py. One-line registry edit + keywords + description.

After that we start #2 — build the test workflow, which sets the pattern for
all future model-driven ADK-based workflows.
