# NINE — Core Workflow Gap Analysis & Roadmap

## 1. WHAT AGENTS DO: The Full Task Taxonomy

### A. CODING / DEVELOPMENT (the maker lane)
1.  **Build/Implement** — write code to solve a task (function, module, app, API)
2.  **Fix/Debug** — diagnose a failure/root cause, then fix it
3.  **Test** — write test cases; run tests; diagnose test failures
4.  **Review/Audit** — review code for quality, bugs, security, style
5.  **Refactor** — improve code structure while preserving behavior (verify with tests)
6.  **Migrate/Port** — convert code from one language/framework to another (verify equivalence)
7.  **Document/Docgen** — generate docs from code (README, API ref, user guide)
8.  **Lint/Format** — check and fix style/convention violations

### B. RESEARCH / INFORMATION (the scholar lane)
9.  **Research/Synthesize** — gather information, synthesize findings into a report
10. **Fact-check/Verify** — check claims against sources, flag unsupported ones
11. **Summarize** — distill a long document into a structured summary
12. **Compare/Evaluate** — compare 2+ options against criteria, produce a recommendation
13. **Compare/A-B** — compare two versions/approaches head-to-head
14. **Timeline/History** — reconstruct events/changes from logs/history

### C. WRITING / DRAFTING (the scribe lane)
15. **Draft Document** — write a spec, proposal, report, design doc, article
16. **Draft Email/Message** — write a reply, outreach, announcement
17. **Draft→Review→Revise** — multi-round editing loop with quality gate
18. **Copywrite** — marketing or UX copy with specified tone
19. **Translate** — translate text while preserving meaning/intent

### D. DATA / ANALYSIS (the analyst lane)
20. **Data Analysis** — explore a dataset, find patterns, produce insights
21. **Extract** — extract structured data from unstructured text (entities, fields, JSON)
22. **Transform** — transform data (format conversion, sorting, filtering, aggregation)
23. **Report/Dashboard** — generate a formatted report/dashboard from data
24. **Validate** — validate data against a schema/constraints; report violations
25. **Visualize** — generate a chart/graph/plot from data

### E. PLANNING / STRATEGY (the strategist lane)
26. **Plan** — break a project/task into ordered steps (task decomposition)
27. **Estimate** — estimate difficulty/effort/risk for tasks
28. **Prioritize** — rank tasks by value/urgency/difficulty
29. **Roadmap** — produce a timeline/phase/milestone plan
30. **SWOT/Risk Analysis** — assess strengths/weaknesses/risks/strategy

### F. AUTOMATION / ORCHESTRATION (the operator lane)
31. **Monitor** — poll a source/system, detect anomalies, report
32. **Triage** — classify incoming items (inbox, alerts, tickets), route them
33. **Pipeline/ETL** — extract → transform → load multi-step data flow
34. **Deploy/Release** — build → deploy → health-check → report
35. **Scheduled/Cron** — run a task on a schedule (recurring)
36. **Repair** — detect a broken system → fix → verify (self-healing)

### G. COMMUNICATION (the comms lane)
37. **Respond/Q&A** — answer a question directly (with evidence)
38. **Draft Reply** — given a message, draft a response
39. **Summarize Meeting** — distill a conversation/meeting transcript
40. **Status Report** — aggregate project state into a status update

### H. KNOWLEDGE / LEARNING (the scholar/teacher lane)
41. **Teach/Lesson** — extract reusable lessons from a completed task
42. **Onboard** — read a codebase/org context, produce a onboarding guide
43. **Classify** — categorize an item against a taxonomy
44. **Glossary** — build a glossary from arbitrary text

### I. PROOF / VERIFICATION (the auditor lane)
45. **Verify** — check that an artifact meets its criteria (gate)
46. **Score/Evaluate** — score an artifact against a rubric
47. **Trace/Audit** — reconstruct decision/execution trail

## 2. HOW PEOPLE USE AGENTS: Real-World Patterns

### Coding assistants (Cursor, Cline, Aider, Claude Code)
- "Fix this bug" → debug → patch → run tests
- "Implement this feature" → plan → build → test → self-verify
- "Review this PR" → analyze → comment → approve/request-changes
- "Add tests for this" → understand → generate → run
- "Refactor this" → analyze → restructure → verify behavior
- "Explain this code" → analyze → respond

### Research assistants (Perplexity, Gemini Deep Research, ChatGPT web)
- "Research X" → search → read → synthesize → cite
- "Compare A vs B" → research → compare → recommended
- "Summarize this paper" → digest → distill

### Data analysts (code-interpreter, Jupyter agents)
- "Analyze this data" → load → explore → visualize → report
- "Find outliers" → query → detect → summary
- "Generate report from this data" → query → format → output

### Ops / DevOps (CI/CD agents, monitoring bots)
- "Deploy to staging" → build → release → healthcheck → report
- "Check logs for errors" → parse → analyze → alert
- "Restart the service" → action → verify health

### Customer support (intercom/ticketing agents)
- "Handle this ticket" → understand → search KB → draft reply
- "Triage the inbox" → classify → prioritize → route
- "Summarize this conversation" → parse → distill

### Knowledge management (RAG / second-brain)
- "What do we know about X?" → search memory → synthesize → respond
- "Extract key points" → read → summarize → persist
- "Update the knowledge base" → learn → categorize → write

### Project management (planning agents)
- "Break down this project" → decompose → estimate → sequence
- "What's the status?" → query → aggregate → report
- "Prioritize these" → analyze → rank

### Security / QA (security bots, test agents)
- "Audit this code" → scan → analyze → report
- "Run tests and fix failures" → test → debug → patch → retest
- "Check compliance" → analyze → verify → report

## 3. NINE CURRENT STATE: What's live vs missing

### Live single-hop workflows (WORKFLOWS dict)
| ID | Route lane | Status | Gated by |
|----|-----------|--------|----------|
| research | research | ✅ works (bash research.md → summarize → HANDOFF.md) | research.md + HANDOFF.md |
| build | build | ✅ works (ADK agent → solution.py → self-test EVAL.json) | solution.py + EVAL.json |
| review | review | ✅ works (bash → review.md + EVAL.json) | review.md + EVAL.json |
| teach | teach | ✅ works (gemma → TEACH.md) | TEACH.md |
| respond | respond | ✅ works (gemini → RESPONSE.md) | RESPONSE.md nonempty |

### Live chains (CHAINS dict)
| ID | Hops | Status |
|----|------|--------|
| research-plan-build-review-teach | research → plan → build → review → teach | ✅ flagship |
| inbox-triage-task-report | triage → task → report | ✅ demo (bash only) |

### Missing from WORKFLOWS (but defined in flagship.py)
- **plan_hop** is DEFINED but NOT in WORKFLOWS registry → routing to "plan" fails

### GAPS (high-impact missing core workflows)
| Proposed wf | What it does | Similar real agent task | Priority |
|-------------|-------------|----------------------|----------|
| test | Write/run tests; diagnose failures; produce results+EVAL.json | "add tests for X", "run the test suite" | HIGH |
| debug | Given an error/failure → root-cause → propose fix → patch | "fix this bug", "why did this fail" | HIGH |
| summarize | Take a doc/text → structured summary (currently only a node) | "summarize this" | HIGH |
| compare | Take 2+ options → criteria comparison → recommendation | "compare A vs B" | MED |
| extract | Unstructured text → structured JSON/table | "extract entities from..." | MED |
| transform | Input file → transform → output file | "convert this CSV to JSON" | MED |
| refactor | Analyze code → improve structure → verify tests still pass | "refactor this module" | MED |
| document | From code/spec → generate docs (README, API ref) | "document this API" | MED |
| migrate | Source language → target language → verify equivalence | "port this from JS to TS" | LOW |
| analyze | Dataset → explore → insights → report.md | "analyze this data" | MED |
| draft | Write a spec/proposal/email/report with draft→review→revise loop | "draft a proposal" | MED |
| plan | Register the existing plan_hop as a standalone wf | "break this down" | HIGH (easy fix) |
| triage | Standalone triage (classify → prioritize → route), not just a chain | "triage my inbox" | LOW (in chain) |
| deploy | Build → deploy → health-check → report | "deploy to staging" | LOW (infra-only) |

## 4. PROPOSED CORE WORKFLOW SET (for beefing up)

### Tier 1 — Must-have cores (universal agent primitives)
1.  **research** ✅ — gather + synthesize → research.md + HANDOFF.md
2.  **plan** ← register plan_hop — decompose task → PLAN.md
3.  **build** ✅ — implement solution → solution.py + EVAL.json
4.  **test** ← NEW — write/run tests → results.md + EVAL.json
5.  **review** ✅ — audit → review.md + EVAL.json
6.  **debug** ← NEW — root-cause + fix → fix.md + patched code + EVAL.json
7.  **summarize** ← NEW standalone — distill → SUMMARY.md
8.  **respond** ✅ — answer question → RESPONSE.md
9.  **teach** ✅ — lesson extraction → TEACH.md

### Tier 1 additions (fills the "everything an agent does" gap)
10. **extract** — text → structured OUTPUT.json
11. **compare** — 2+ options → RECOMMENDATION.md with scoring matrix
12. **transform** — INPUT{format} → OUTPUT{format} + EVAL.json
13. **draft** — spec/proposal/email → DRAFT.md + review→revise loop

### Tier 2 — Specialized but high-value
14. **refactor** — code → improved code + tests still pass + REPORT.md
15. **document** — code/spec → README.md / API.md
16. **analyze** — data → INSIGHTS.md + charts
17. **migrate** — source → target + equivalence test

### Tier 3 — Ops/automation (for Taskmaster track demo)
18. **deploy** — build → deploy → health-check → REPORT.md
19. **monitor** — poll → detect → ALERT.md
20. **triage** — classify + prioritize → TRIAGE.md

## 5. NEXT STEPS (proposed order)

1. **Register plan_hop in WORKFLOWS** (one-line fix — it's already built)
2. **Build the test workflow** — highest impact gap: "write tests for X" → run → results
3. **Build the debug workflow** — "fix this" → root-cause → patch → verify
4. **Build summarize standalone** — promote summarizer node to its own workflow
5. **Build extract workflow** — structured data extraction
6. **Build compare workflow** — criteria-based comparison + recommendation
7. **Build transform workflow** — format conversion
8. **Build draft workflow** — draft → review → revise loop
9. **Beef up existing hops** — research (multi-source), build (multi-file), review (security+quality)
10. Remodel chains to compose the new cores (e.g. build-test-debug-review chain)
