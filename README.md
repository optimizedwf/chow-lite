# chow-lite 🍜
<p align="center">
  <img src="https://img.shields.io/github/actions/workflow/status/optimizedwf/chow-lite/ci.yml?branch=main&label=CI" alt="CI">
  <img src="https://img.shields.io/badge/tests-63%20passing-brightgreen" alt="tests">
  <img src="https://img.shields.io/badge/coverage-80%25-yellow" alt="coverage">
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="license">
</p>



**A router-first, evidence-gated agent operating system.**
Tasks come in → the router picks the right workflow → the workflow runs →
the evidence gate refuses to say "done" without proof.

```
  ROUTE → EXECUTE → VERIFY → LEARN
```

> "An exit code is not success. A receipt is UNVERIFIED until the expected
> artifact and validation produce an evidence-backed SHIP/FIX/BLOCK."

chow-lite is the open-source, lightweight version of the agent harness that
runs our own multi-lane agent operation. It is built **on Google ADK 2.0 +
Gemini 3.5 Flash + Cloud Run + Firestore** — no vendor lock-in, MIT licensed,
self-hostable, scale-to-zero.

---

## Architecture

![chow-lite architecture](docs/architecture.png)

**One loop, four phases, zero blind trust:**

| Phase | What happens | Backing tech |
|---|---|---|
| **ROUTE** | every task is classified to a workflow (intent router) | Gemini 3.5 Flash via ADK + deterministic keyword fallback |
| **EXECUTE** | declarative workflow DAGs run typed nodes (`prompt`/`bash`/`tool`/`subagent`) | Google ADK 2.0 agents, artifact-passing contract |
| **VERIFY** | evidence gate checks EVAL.json, required artifacts, exit codes | verdict: **SHIP / FIX / BLOCK** |
| **LEARN** | route events -> improvement candidates (human-approved only) | append-only event store, never auto-applies |

Multi-hop **chains** hand off artifacts between departments with a gate at
every handoff — nothing ships without evidence, at any stage.

## Quickstart

```bash
pip install -e .            # or: uv pip install -e .
export GEMINI_API_KEY=...   # optional; deterministic routing works without it
chow submit "research the history of the typewriter"
chow chain demo "respond to customer refund question"   # 3-hop demo lane
chow chain flagship "build a calculator"                # 5-hop full chain
chow stats
```

Everything ships with a full artifact trail and a SHIP/FIX/BLOCK verdict per
job — see `chow status <job_id>` and `chow artifacts <job_id>`.

## Submission pack (All Things Agentic 2026)

Devpost-ready description, judging-rubric mapping, demo-video script, and the
3 human-only setup steps: see [SUBMISSION.md](SUBMISSION.md) and
[docs/ADAM-RUNBOOK.md](docs/ADAM-RUNBOOK.md).

## Why this exists

Most "agents" are chatbots in a trench coat: they talk, they don't do.
And the ones that do act usually **claim success without proof** — a script
exits 0 and everyone assumes it worked.

chow-lite makes agents *trustworthy by construction*:

1. **ROUTE** — every task is classified to a workflow by an intent router
   (Gemini 3.5 Flash, with a deterministic keyword fallback so the core
   works offline and in CI).
2. **EXECUTE** — workflows are declarative DAGs of typed nodes
   (`prompt` / `bash` / `tool` / `subagent`), with artifacts passed between
   nodes under a JSON-schema contract.
3. **VERIFY** — an evidence gate runs checks (EVAL.json, required artifacts,
   exit codes) and returns a verdict: **SHIP / FIX / BLOCK**. A job is only
   `shipped` when the evidence passes — everything else is UNVERIFIED.
4. **LEARN** — every route decision and job outcome is logged to a durable
   ledger; workflow stats feed back into better routing. Candidate-only
   self-improvement: nothing promotes until it passes the regression suite.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .

chow submit "research the latest agent frameworks"
# → route decision (workflow_id: research)
# → workflow runs, produces research.md + EVAL.json
# → evidence gate: SHIP (all evidence checks passed)
# → job ledger: submitted → routing → running → awaiting_evidence → shipped

chow discover              # list jobs
chow status <job_id>       # full job record + verdicts
chow artifacts <job_id>    # artifact manifest (sha256, size, producer)
chow cancel <job_id>       # cancel
chow recover <job_id>      # recover a blocked/failed job
chow stats                 # ledger stats
```

## Required tech (hackathon rules compliance)

| Requirement | chow-lite uses |
|---|---|
| Gemini 3.5 or newer | Gemini 3.5 Flash via Gemini API (router + agent steps) |
| Google agent framework | **Google ADK 2.0** (agents, routing, workflow-agents, sessions/memory, evaluate, observability) |
| Google Cloud infra service | **Cloud Run** (scale-to-zero deployment) + **Firestore** (durable job ledger, memory, route events) |

## Architecture

```
[Task] → [Router: Gemini 3.5 Flash classification]
              │ route-decision (schema-validated)
              ▼
[Workflow DAG: ADK workflow-agents (sequential/parallel/loop)]
   nodes: prompt | bash | tool | subagent  — artifact passing (schema)
              │
              ▼
[Evidence Gate: ADK evaluate + EVAL.json + exit codes]
   verdict: SHIP | FIX | BLOCK   (exit code ≠ success)
              │
              ▼
[Job Ledger: Firestore/JSONL]  submit→routing→running→awaiting_evidence→shipped
              │
              ▼
[Learn: route events → workflow stats → better routing (candidate-only)]
```

Deployment: `deploy/cloud-run.yaml` (scale-to-zero), Firestore for state,
ADK agents in the user-facing build hop (LlmAgent + FunctionTool) with an
independent bash self-test writing EVAL.json from the real run result.
Secret hygiene by design: redaction in logs, reference-only paths, never
commit credentials.

## Repository layout

```
chowlite/
  router/classifier.py    intent classifier + route-decision contract
  ledger/ledger.py        durable job ledger (JSONL + Firestore adapters)
  gates/evidence.py       SHIP/FIX/BLOCK evidence gate (EVAL.json, exit codes)
  schema_validation.py    JSON Schema checks at every boundary (jsonschema)
  runtime/workflows.py    declarative workflow DAG executor
  runtime/adk_runtime.py  Google ADK 2.0 integration layer
  cli.py                  operator CLI (submit/status/discover/...)
schemas/                  JSON Schemas: route-decision, agent-job, verdict, artifact
learn/                    route-event store + improvement candidates
chains/                   chain engine + flagship 5-hop chain + demo lane
workflows/                example workflow DAGs
deploy/                   Cloud Run + Firestore config (FastAPI operator API)
docs/                     architecture diagram (SVG + PNG)
tests/                    25 tests (router, ledger, gates, executor, chains, learn, ADK)
```

## Roadmap

- [x] Core loop (router → workflow → gate → ledger) with 22 passing tests
- [x] Google ADK 2.0 agent integration (agents as `subagent` nodes)
- [x] 5-hop chain (research → plan → build → review → teach) + demo lane
- [x] Cloud Run + Firestore deploy layer (Dockerfile, service, rules, API)
- [x] Route-event learning loop (append-only JSONL + candidate-only learner)
- [x] Architecture diagram + demo lane + one-command `python demo.py`
- [x] Second Google model: Gemma 4 teach hop (+0.2 Stage-3 bonus, live-tested)
- [ ] Live Cloud Run deployment (needs Adam to auth gcloud — runbook in docs/ADAM-RUNBOOK.md)
- [ ] Demo video with live GCP proof (script in docs/demo-script.md)

## License

MIT © 2026 Adam Norman & Chow
