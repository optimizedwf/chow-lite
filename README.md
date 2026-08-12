# chow-lite 🍜

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
# → workflow runs, produces FINAL_REPORT.md + EVAL.json
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
ADK observability for traces. Secret hygiene by design: redaction in logs,
reference-only paths, never commit credentials.

## Repository layout

```
chowlite/
  router/classifier.py    intent classifier + route-decision contract
  ledger/ledger.py        durable job ledger (JSONL + Firestore adapters)
  gates/evidence.py       SHIP/FIX/BLOCK evidence gate (EVAL.json, exit codes)
  runtime/workflows.py    declarative workflow DAG executor
  runtime/adk_runtime.py  Google ADK 2.0 integration layer
  cli.py                  operator CLI (submit/status/discover/...)
schemas/                  JSON Schemas: route-decision, agent-job, verdict, artifact
chains/                   5-hop chain: research → plan → build → review → teach
workflows/                example workflow DAGs
deploy/                   Cloud Run + Firestore config
tests/                    17 tests (router, ledger, gates, executor)
```

## Roadmap

- [x] Core loop (router → workflow → gate → ledger) with 22 passing tests
- [x] Google ADK 2.0 agent integration (agents as `subagent` nodes)
- [x] 5-hop chain (research → plan → build → review → teach) + demo lane
- [x] Cloud Run + Firestore deploy layer (Dockerfile, service, rules, API)
- [ ] Live Cloud Run deployment (needs billing-enabled GCP project)
- [ ] Firestore-backed ledger + route-event learning loop
- [ ] Architecture diagram + demo video

## License

MIT © 2026 Adam Norman & Chow
