# chow-lite — All Things Agentic 2026 Submission Pack

> Track: **TASKMASTER** — "Bring Your Own Friction": chow-lite eats its own
> dogfood. The agent that built this repo used chow-lite-style discipline
> (route → execute → verify → learn) to ship it.
> `created for purposes of entering this hackathon` — see git history (all
> commits Aug 3–31, 2026 submission period).

---

## 1. Devpost title (≤60 chars)
**chow-lite — an evidence-gated agent OS (ROUTE→EXECUTE→VERIFY→LEARN)**

## 2. Tagline
Open-source agent operating system on Google ADK 2 + Gemini 3.5 Flash: every
task is routed, executed by a typed workflow, verified by an evidence gate,
and logged for learning. Runs on Cloud Run + Firestore. No chatbot wrap —
a real autonomy kernel.

## 3. Description (paste into Devpost "Describe your project")

**The problem.** Most "agent" demos are single-prompt chatbot wraps: one LLM
call, one answer, no proof it did anything. Real autonomous work needs a
**kernel**: decide what to do, do it in a typed pipeline, *prove* it happened,
and get smarter from the trail.

**chow-lite** is that kernel, in ~2700 lines of Python, open source (MIT):

- **ROUTE** — a Gemini 3.5 Flash router (with deterministic keyword fallback)
  turns free-text tasks into typed `RouteDecision`s against a workflow catalog.
- **EXECUTE** — Google ADK 2 `LlmAgent` + `FunctionTool` run typed workflows
  (2–5 hops: `research→plan→build→review→teach`, or the Taskmaster demo lane
  `inbox→triage→task→report`). Every hop runs in the same job dir with
  full artifact handoff — no invisible state.
- **VERIFY** — an evidence gate per hop checks *required artifacts* (content,
  size, sha256) and *exit codes*; verdicts are SHIP / FIX / BLOCK with JSON
  Schema validation. Fix loops retry FIX hops up to N times, then BLOCK.
- **LEARN** — every route event is appended to an append-only JSONL store;
  the learner proposes **candidate-only** improvements (it never silently
  changes behavior — the human/operator applies them).

**Production shape.** Cloud Run + Firestore backend (FastAPI operator API:
`/health`, `/v1/submit`, `/v1/jobs`, `/v1/stats`), Dockerfile, `gcloud`
one-command deploy, scale-to-zero, secure API-key auth. Works offline with
zero API keys (deterministic routing + bash hops) — CI-friendly and
judge-friendly.

**Dogfooding.** The repo's own roadmap, tracker, and this submission were
managed with the same route→execute→verify→learn loop. 63/63 tests pass (5 live-gated skips);
every CLI run ships a full artifact trail.

**Try it (5 minutes, no key needed):**
```bash
git clone https://github.com/optimizedwf/chow-lite
cd chow-lite && pip install -e .
python demo.py "plan a weekend trip to Big Sur"    # full loop, SHIP verdict
chow chain flagship "build a calculator"            # 5-hop chain
```

## 4. Architecture diagram
`docs/architecture.svg` (also rendered as `docs/architecture.png`).

## 5. Demo video (≤4:00, public YouTube)
Script in `docs/demo-script.md`. Must include **live GCP proof**:
Cloud Run dashboard / `.run.app` URL in a browser + a live submit through
the deployed API.

## 6. Links
- Repo: https://github.com/optimizedwf/chow-lite (public, MIT)
- Live site: https://chow-lite-<hash>.run.app (fill after deploy)
- YouTube: (fill after upload)

## 7. Required tech checklist (all three mandatory)
- [x] **Gemini 3.5+ via Gemini API** — `gemini-3.5-flash` (live-tested router
      + ADK agent; see `tests/test_router_live.py`, `tests/test_adk.py`)
- [x] **Google agent framework: ADK** — `google-adk` 2.6.x, `LlmAgent`,
      `FunctionTool`, `InMemoryRunner`
- [x] **Google Cloud infra: Cloud Run + Firestore** — `deploy/` (server,
      cloud-run.yaml, firestore.rules, deploy.sh)

## 8. Judging rubric mapping
| Criterion (weight) | How chow-lite hits it |
|---|---|
| Innovation & Operational Utility (40%) | autonomy kernel with evidence-gated SHIP/FIX/BLOCK; candidate-only learning loop; dogfooded on its own build |
| Architectural Discipline & Tech Stack (30%) | typed schemas for every boundary (jobs, artifacts, verdicts, route decisions/events); ADK + Gemini 3.5 + Cloud Run/Firestore; JSON Schema validation in tests |
| Demo & Production Readiness (30%) | one-command demo, 63 tests, Dockerfile + deploy.sh, FastAPI operator API, offline fallback |

## 9. Post-submission freeze
Video, repo, and live site are frozen after submission (Aug 31 17:00 PDT);
any fix during judging risks prize eligibility. Submit once, verify the
Devpost form, then stop editing.

## 10. Adam-only actions (blockers)
See `docs/ADAM-RUNBOOK.md`.
