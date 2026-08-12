# Demo video script — chow-lite (≤4:00, public YouTube, LIVE GCP PROOF required)

> Rules: only first 4 minutes evaluated; English; must show live Google Cloud
> proof in footage (Cloud Run dashboard / .run.app URL in browser / Vertex logs).
> Fresh video — do NOT reuse the DataHub Mr Chow film.

---

## Timeline (target 3:30 total)

### 0:00–0:15 — Hook (title card + one line)
- Card: "chow-lite — an evidence-gated agent OS"
- VO: "Most agent demos are chatbot wraps — one prompt, one answer, no proof.
  chow-lite is an agent operating system: it routes, executes, **verifies with
  evidence**, and learns. Built for this hackathon on Google ADK 2, Gemini 3.5
  Flash, and Cloud Run."

### 0:15–0:50 — ROUTE (terminal, live)
- `chow submit "research the history of the printing press"`
- Show `chow status <job>` → decision JSON: workflow_id=research, confidence,
  reason. VO: "A Gemini 3.5 Flash router turns free text into a typed route
  decision against a workflow catalog — with deterministic fallback when no
  key is set, so judges can run it offline."

### 0:50–1:50 — EXECUTE (terminal, live, the money shot)
- `python demo.py "plan a weekend trip to Big Sur"`
- Let it run: inbox → triage → task → report, each hop printing SHIP.
- VO: "The Taskmaster lane: an inbox arrives, the agent triages it, executes
  the task, and writes a report. Every hop is a typed step in Google ADK —
  an LlmAgent calling a FunctionTool — and every hop runs in the same job
  directory, so state flows: the triage output feeds the task, the task
  output feeds the report."
- Cut to `chow artifacts <job>` showing sha256 + sizes.

### 1:50–2:30 — VERIFY (screenshot of EVAL.json + verdict)
- Show `chow chain flagship "build a calculator"` (5 hops) finishing with
  SHIPPED and the artifact rollup.
- Show EVAL.json + the evidence gate checks. VO: "Each hop has an evidence
  gate: required artifacts must exist with content and checksums, exit codes
  must be zero. Verdicts are SHIP, FIX, or BLOCK — fix loops retry, then
  block rather than lie."

### 2:30–3:00 — LIVE GCP PROOF (REQUIRED)
- Browser: open `https://chow-lite-<hash>.run.app/health` → JSON status 200.
- Browser: Cloud Run dashboard showing the chow-lite service + request count.
- `curl -X POST <url>/v1/submit ...` then `curl <url>/v1/jobs/<id>` → SHIP.
- VO: "The same kernel runs on Cloud Run with Firestore — live, scale-to-zero,
  verified by the operator API you just watched execute a real job."

### 3:00–3:30 — LEARN + close
- Show `events.jsonl` route events + learner candidates.
- VO: "Every route decision is logged; the learner proposes improvements —
  candidates only, never silent self-modification."
- Card: repo URL github.com/optimizedwf/chow-lite, MIT, 27/27 tests.
- VO: "An agent OS that proves its work and gets smarter — route, execute,
  verify, learn."

---

## Recording notes
- Record terminal in 4K/60 if possible; use a clean theme, large monospace.
- VO: en-US-GuyNeural or en-US-ChristopherNeural via edge-tts (same as DataHub
  pipeline) then mix with ffmpeg; or record live VO for energy.
- Add captions (YouTube auto-captions OK but review).
- After upload: set PUBLIC (unlisted does NOT count), add "created for
  purposes of entering this hackathon" to description, link on Devpost form.
