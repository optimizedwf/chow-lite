# nine — All Things Agentic 2026: Score-Maximization Strategy

> Prepared 2026-08-12 · Product strategy + rules audit for the TASKMASTER track.
> Rules verified against the LIVE official pages (fetched 2026-08-12):
> `allthingsagentichackathon.devpost.com/rules` and `/details/faqs` (Resources tab).
> 19 days remain before the Aug 31 17:00 PDT deadline. The system works end-to-end
> (32 offline tests pass, 5 live tests gated on GEMINI_API_KEY, demo lane + 5-hop
> flagship chain run, 3-segment demo video exists). Do NOT overbuild.

---

## 0. Bottom line

The submission is in **no DQ territory** on the mandatory stack, but it has **two
Stage-1-grade gaps** (live GCP proof not yet recorded; ADK not exercised in any
main path) and **one credibility gap** (the demo's EXECUTE hops are canned bash —
a judge who reads the code sees scripts, not an autonomous agent). Fix those
three, tighten the Devpost copy, and the score moves from "solid 3/5" to "top-decile
4+/5". Everything below is ranked; the top 5 this week are at the end.

---

## 1. Rules check (verified against the official rules + FAQ)

### Mandatory stack — all three are satisfied, with caveats

| Requirement (rules §6) | nine | Verdict |
|---|---|---|
| Gemini 3.5 or newer via **Gemini API or Vertex AI** | `gemini-3.6-flash` via `google-genai` client (router: `nine/router/classifier.py`; live tests: `tests/test_router_live.py`; ADK agent: `tests/test_adk.py`) | ✅ verified in code |
| At least one **Google agent framework** (ADK, GenAI SDK, Antigravity, GenKit) | `google-adk==2.6.3` installed; `nine/runtime/adk_runtime.py` (LlmAgent, FunctionTool, InMemoryRunner); live test `tests/test_adk.py` | ⚠️ **ADK exists but is NOT invoked in any user-facing path** — see §2 row 4. This is the weakest spot. |
| At least one **Google Cloud infra service** | Cloud Run (`deploy/cloud-run.yaml`, `Dockerfile`, `deploy/deploy.sh`) + Firestore (`deploy/firestore.rules`, `nine/ledger/firestore_ledger.py`) | ✅ in code; **deployment itself still pending (Adam step)** — see §4-R1 |

### Stage-1 pass/fail checklist (rules §8)
| Requirement | Status |
|---|---|
| Built with required developer tools | ✅ (see table) |
| Select ONE category — TASKMASTER | ✅ plan |
| URL to hosted project (if available) — *highly encouraged* | ⏳ after deploy (`nine-<hash>.run.app`) |
| Text description (features, tech, findings) | ✅ SUBMISSION.md §3 |
| URL to repo (public or private + access) | ✅ public MIT `github.com/optimizedwf/nine` |
| Spin-up instructions in README | ✅ Quickstart + demo.py |
| Architecture diagram | ✅ `docs/architecture.png` + `.svg` |
| Demo video: ≤4:00, public YouTube/Vimeo, English, **backend on Google Cloud visible** | ⚠️ v3 video exists (119.9s) but **GCP-proof segment NOT yet recorded** → **Stage-1 blocker** |
| New project during submission period | ✅ all commits 2026-08-12 (window Aug 3–31) |
| One prize max / unique submission | ✅ one submission planned |

### Stage-2 rubric (weighted, 1–5 each → averaged)
- **Innovation & Operational Utility (40%)** — "eliminate real-world friction? Is the
  *Twist* present? high-value autonomous execution over simple chat queries."
  For Taskmaster ("Continuous Action Engine"): *"Does the agent successfully intercept
  and complete a multi-step background workflow **without human intervention**? Did the
  team successfully utilize the **Bring Your Own Friction** (BYOF) mandate to solve a
  unique, personal problem?"*
- **Architectural Discipline & Tech Stack (30%)** — decoupled systems, state
  management, failure-tolerant; Taskmaster: "clean, modularized... tools properly
  isolated and scoped for security."
- **Demo & Production Readiness (30%)** — 4-min video clarity (friction + architecture);
  **Proof of Action: "unedited, live execution of the agent performing its task (terminal
  logs, database updates, or UI changes)"**; documentation + architecture diagram;
  **visual proof of Google Cloud deployment in the video.**

### Stage-3 bonus (rules §6/§8 — final score is 1–6)
| Bonus | Points | nine status |
|---|---|---|
| Blog/podcast/video about the build (public, must say "created for purposes of entering this hackathon") | +0.2 | ❌ not done (easy) |
| Social post X/LinkedIn/IG/FB (#AllThingsAgenticHackathon on X/LinkedIn) | +0.2 | ❌ not done (easy) |
| Each additional Google AI model (Gemma/Veo/Lyria) | +0.2 each, **capped at +0.6** (3 models) | ✅ Gemma 4 teach hop = +0.2; **runbook's model-cap wording is wrong** (says "+0.2 each, max +1.0" — actual model cap is 0.6; total bonus cap 1.0 = 0.2+0.2+0.6) |

---

## 2. Claim-vs-code audit (what a judge reading the repo actually sees)

| # | Claim (where) | Reality (code) | Action |
|---|---|---|---|
| 1 | "27/27 tests pass" (SUBMISSION.md), "25 tests" (README), "30/30" (git log) | **37 tests collected** — 32 pass offline, 5 skip without key (ADK, 2×Gemma live, 2×router live) | Unify: **"37 tests (32 offline + 5 live with GEMINI_API_KEY)"** |
| 2 | "~1,200 lines of Python" (SUBMISSION.md) | Product code = **2,293 LOC** (nine/ + deploy/ + workflows/ + demo) excl. tests | Update or scope ("core loop ~1,200 LOC, ~2,300 total") |
| 3 | "Gemini 3.5 Flash **via ADK**" (README router row) | Router uses raw `google.genai` client, **not ADK** | Fix wording: "Gemini 3.5 Flash via Gemini API + ADK agent nodes" |
| 4 | "Google ADK 2.0 (agents, routing, workflow-agents, sessions/memory, evaluate, observability)" (README) | ADK appears **only in `adk_runtime.py` + `tests/test_adk.py`**. `cli.py`, `demo.py`, `demo_live.py`, `deploy/server.py` never call ADK. No observability code anywhere | **Wire ADK into the main demo path (§5-P1). Drop the "observability" claim or add a minimal trace log** |
| 5 | "LEARN: every route event is appended to an append-only JSONL store" | Only `ChainExecutor` records events. `cli submit`, the deployed API, and `demo.py`'s default path do **not**. Demo uses a temp dir → **all state vanishes**; deployed API records **zero** route events | Wire Learner into server + CLI; persistent store; `/v1/events` (§5-P2) |
| 6 | "live GCP proof: real Gemini routing on the deployed API" (demo-script) | `deploy/deploy.sh` sets `GEMINI_MODEL` + `FIRESTORE_COLLECTION` but **NOT `GEMINI_API_KEY`** → deployed router runs **keyword fallback**, contradicting the video script | Add `GEMINI_API_KEY` to deploy env (from `--from-file` secret) |
| 7 | "5-hop chain + Taskmaster lane" | True, but every EXECUTE hop is **canned bash**: build writes `return 42`, review writes `PASS` unconditionally, EVAL.json is written by the same node it certifies | §5-P1: make the build hop a real Gemini/ADK step with an **independent self-test** writing EVAL.json |
| 8 | "Fix loops retry FIX hops up to N, then BLOCK" | ✅ `chain.py` implements exactly this | keep |
| 9 | "Works offline, zero API keys" | ✅ verified (32 tests pass keyless; `demo.py` runs) | keep |
| 10 | Title "≤60 chars" (SUBMISSION.md) | Actual title = **67 chars** → Devpost truncates/rejects | Shorten (§6) |
| 11 | "Cloud Run + Firestore FastAPI API (/health, /v1/submit, /v1/jobs, /v1/stats)" | ✅ `deploy/server.py` | keep; add `/v1/events` (§5-P2) |
| 12 | Roadmap: "Live Cloud Run deployment" + "Demo video with live GCP proof" unchecked | Both are the **only two remaining blockers** | Do first (§4-R1) |
| 13 | "built... for purposes of entering this hackathon — all commits Aug 3–31" | ✅ verified: first commit 2026-08-12 00:21 | keep; add explicit pre-existing-code disclosure ("concepts, not code") |

---

## 3. Innovation gaps — candidates evaluated

Rubric anchor: *"high-value autonomous execution over simple chat queries"* + BYOF.
The current **Twist** (evidence-gated SHIP/FIX/BLOCK; "an exit code is not success")
is genuinely memorable — **protect and sharpen it**. The score leak is that EXECUTE
is scripted and LEARN is inert.

| Candidate | Verdict | Why |
|---|---|---|
| (a) LEARN that **applies** validated improvements w/ rollback | ✅ **YES — minimal form** | Full auto-apply contradicts the "human owns changes" brand and adds risk. A `nine learn apply <candidate>` command that (1) runs the regression suite, (2) writes the change to a git-tracked config, (3) is reversible by git revert, converts "candidate-only" from a limitation into "self-improvement with safety rails" — a stronger story than either extreme. ~1 day. |
| (b) Cross-run memory / run-to-run improvement | ✅ **YES — cheap, high visibility** | Today demo state lives in a temp dir; the system cannot demonstrably improve. Persist route events (JSONL local / Firestore cloud), default demo dirs to repo-local `work/`/`jobs/`, and add a demo beat: **run 1** routes to fallback (conf 0.2) → learner proposes a keyword → apply → **run 2** routes correctly (conf 0.95). That is the "gets better every run" money shot. ~0.5–1 day. |
| (c) Self-improving router (keyword catalog from route events) | ✅ **YES — fold into (a)+(b)** | The `Learner` schema has `kind="keyword"` but `learn()` never emits one. Add: low-confidence route to `fallback-respond` → keyword candidate. ~0.5 day + test. |
| (d) Multi-agent delegation demo (ADK sub-agents) | ⚠️ **Yes but scoped — only after P1/P2** | ADK `LlmAgent(sub_agents=[...])` in the flagship chain (research agent + build agent) both proves ADK in the main path and adds "intelligent delegation" language. ~2 days. Do only if time remains after §5-P1 (which already puts ADK in the main path). |
| (e) A "second workflow" beyond flagship+demo | ⚠️ **Already done — upgrade, don't add** | Demo lane + `workflows/research_demo.py` exist. A third *canned* lane adds zero innovation. The upgrade is making one lane **real** (real tool calls / real data), which is exactly §5-P1. |
| (f) Confidence-calibrated fallback | ✅ **YES — cheap** | Router already falls back when model output is unparsable. Add a threshold: `confidence < 0.35` → keyword fallback with explicit "low-confidence escalation" reason + a learner candidate. 0.5 day. Nice robustness talking point for Architecture. |
| (g) Observability / dashboard | ⚠️ **Minimal only** | `/v1/events` endpoint + a tiny static HTML status page + structured per-job trace log lines. Skip full OpenTelemetry/ADK-observability wiring (time). ~1 day. |

### Top 3 to build (see §5 for sketches)
1. **Real agentic EXECUTE** (P1) — one genuinely model-driven hop with independent verification.
2. **LEARN that visibly improves the system** (P2) — persistent events + keyword candidates + apply-with-rollback + `/v1/events`.
3. **Demo narrative + submission hygiene** (P3) — hook/FIX-beat/close, GCP proof committed to repo, all claim fixes, then bonus points.

---

## 4. Risks (DQ / points loss)

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | **No live GCP proof in the video** → Stage-1 fail ("strict proof... built and deployed there"). FAQ: capture proof in the video **and back it up in the code repository** | 🔴 DQ-grade | Deploy first (fix `deploy.sh` to include `GEMINI_API_KEY`), record: (1) browser with `.run.app` URL, (2) 3–5s Cloud Run dashboard/console shot, (3) `demo_probe.py` transcript; commit `docs/GCP-PROOF.md` (URL, timestamps, screenshots, transcript) into the repo |
| R2 | **Credit form not submitted / wrong track name / too-short pitch** → auto-declined, no $150 | 🔴 | Submit before **Aug 28 12:00 PT** via Resources-tab link `forms.gle/5PtXmw1dSbDnpYke9` (rules PDF still shows the old link — runbook already noted this). Track must read exactly **TASKMASTER**; 1–2 sentence pitch (draft exists in runbook) |
| R3 | **Post-submission freeze**: any edit to video/repo/live site during judging (Sep 1–Oct 1) risks prize | 🔴 | Finish everything by **Aug 30**; submit once; verify all fields; fork the repo if you want to keep building |
| R4 | **Team eligibility**: every member must be an eligible individual on Devpost | 🟠 | List only humans (solo `optimizedworkflowdev` is cleanest). Do **not** list an AI agent as a teammate. LICENSE copyright line "Adam Norman & Nine" is fine. |
| R5 | **Claim credibility**: stale numbers (27/27, 1,200 LOC), title >60 chars, "ADK observability", "Gemini via ADK" — judges read code | 🟠 | §2 table — fix all; cheap and fast |
| R6 | **New-project rule / pre-existing code**: internal nine harness could look pre-existing | 🟠 | All commits in-window ✅; keep the "rebuilt fresh... concepts, not code" disclosure; add an explicit disclosure line in SUBMISSION.md |
| R7 | **Bonus-cap misreading**: model bonus capped at **0.6** (3 models), total bonus 1.0 | 🟡 | Correct the runbook text; Gemma = +0.2 already; blog + social = +0.4 nearly free; Veo/Lyria (+0.4 more) only last |
| R8 | **Deployed API not actually using Gemini** (no key in env) → video claim false | 🟠 | Fix `deploy.sh`; verify `/health` and a submit show `router: gemini-3.6-flash-live` |
| R9 | **Judges may judge from description+video only** | 🟡 | Devpost description must be self-sufficient and accurate (video = repo = description) |
| R10 | **YouTube upload not public / blog unlisted** | 🟡 | Both must be PUBLIC (not unlisted) per rules |
| R11 | Video >4:00 (only first 4 min evaluated) | 🟢 | Current plan 2:25 — fine; keep GCP segment ≤2:25 total |

---

## 5. Top 3 innovation builds (concrete sketches)

### P1 — Real agentic EXECUTE: flagship "build" hop becomes genuinely model-driven (IMPACT: high · EFFORT: M, 2–3 days)
- Replace the canned `build` bash node in `nine/chains/flagship.py` with an **ADK `LlmAgent`** (`model=Gemini(model="gemini-3.6-flash")`, `tools=[FunctionTool(run_python), FunctionTool(read_task)]`) that reads `task.txt`+`PLAN.md`, writes real `solution.py`, and runs it.
- **Independence is the point**: EVAL.json is written by a *separate* `self-test` node that executes the code and records pass/fail — the coding node must NOT write its own EVAL.json (today it does; a judge will notice self-certification). Gate then requires `eval-json` + `exit-codes` as today.
- Offline fallback: keep the deterministic node when no key is present (a `make_build_hop(with_adk=bool(key))` switch) so CI/offline tests stay green.
- Bonus within same effort: research hop gains a `FunctionTool` that fetches one real URL — "interacts with different apps" language for the rubric.
- Why: fixes the ADK-in-main-path Stage-1 weakness (§2-4), delivers "Proof of Action" (unedited live execution producing real code + real test results), and is the single biggest Innovation point gain.

### P2 — LEARN that demonstrably improves the system (IMPACT: high · EFFORT: M, 1.5–2 days)
- Persist route events from **every** submit path: `cli.cmd_submit`, `deploy/server.py`, `demo.py`/`demo_live.py` (drop the temp-dir default; use repo-local `jobs/events.jsonl` + Firestore collection in cloud). Add `GET /v1/events` and include events/candidates in `/v1/stats`.
- Add the missing `kind="keyword"` candidate: low-confidence route → "add keyword(s) / re-describe workflow" (0.5 day incl. test).
- Add `nine learn apply <candidate_id>`: runs the regression suite (`pytest tests/ -q -k "not live"`), then writes the router keyword/description change into a git-tracked catalog file (`nine/router/catalog.json`) and commits; `nine learn revert <candidate_id>` restores. Rollback = git revert.
- Demo beat (for video + README): run 1 "zzz qqq unknown task" → `fallback-respond` conf 0.0 → candidate "add keyword" → apply → run 2 routes correctly. Shows run-to-run improvement = "cross-run memory" (candidate b).
- Why: converts the LEARN loop from inert accounting into the differentiating "self-improving agent OS" story; also hardens Architecture (state management across runs).

### P3 — Demo narrative + submission hygiene (IMPACT: med-high · EFFORT: S, 1–2 days)
- 30-second takeaway for judges: **"Most agents say 'done' with no proof. nine is an agent OS that refuses to ship without evidence — it routes, executes, verifies, and learns, live on Google Cloud — and it gets smarter every run."**
- Hook (first 10s): title card over VO: *"Every agent says 'done.' Here's one that has to prove it."* → straight into a **live FIX moment**: run a task whose artifact is missing, watch the gate return FIX/BLOCK, fix, SHIP. No other entry will show its system refusing to succeed — it's the twist made visible.
- Close (last 10s): end card — `ROUTE → EXECUTE → VERIFY → LEARN` · `live on Cloud Run + Firestore` · `.run.app` URL · *"An OS that can't lie to you. Built by the agent OS you just watched."*
- Gaps in current script (`docs/demo-script.md` + v3 video): (1) never names the *friction problem* — add one line of BYOF dogfooding ("this repo was built under its own loop"); (2) no failure/recovery moment; (3) GCP segment is terminal-only — add the Cloud Run dashboard/browser shot (FAQ-preferred proof); (4) LEARN is shown as counts, not as improvement — swap in the run-1/run-2 beat (P2).

---

## 6. Devpost description edits (judge-facing)

1. **Title** — current 67 chars > Devpost 60-char limit. Use:
   **"nine — the agent OS that proves its work"** (43) or **"nine — an evidence-gated agent OS"** (38).
2. **Tagline** — keep, tighten the tail: "...No chatbot wrap — a real autonomy kernel, live on Cloud Run + Firestore, MIT open source."
3. **Tech bullets** — fix to: "37 tests (32 offline + 5 live)"; "~2,300 lines of Python"; name `google-adk 2.6.3` explicitly; add "Gemma 4 = second Google model (Stage-3 bonus)". Keep the 5-minute try-it block (it works).
4. **Rubric mapping** — Innovation row: add BYOF/dogfood line + "applies human-approved improvements with regression-checked rollback" (after P2). Demo row: add "live GCP proof: Cloud Run dashboard + .run.app URL in video and docs/GCP-PROOF.md".
5. **Additions**: hosted URL + YouTube URL placeholders; one-line disclosure: *"Built during the submission period (Aug 3–31, 2026; all commits in-window). Concepts modeled on our internal multi-lane agent harness; code written fresh for this hackathon."*

---

## 7. Prioritized recommendations

| # | Recommendation | IMPACT | EFFORT | When |
|---|---|---|---|---|
| 1 | **Deploy to Cloud Run + Firestore with GEMINI_API_KEY in env; record live GCP proof (browser + dashboard + probe); commit `docs/GCP-PROOF.md`** | HIGH (Stage-1 gate) | S (Adam-gated, ~30 min hands-on) | **Week 1, day 1–2** |
| 2 | **P1: real agentic build hop via ADK + independent self-test** (ADK in main path; proof of action) | HIGH | M (2–3 d) | Week 1–2 |
| 3 | **P2: LEARN end-to-end** (persistent events, keyword candidates, `nine learn apply` w/ regression+rollback, `/v1/events`) | HIGH | M (1.5–2 d) | Week 2 |
| 4 | **Submission hygiene** (title ≤60, test counts 37, LOC 2,300, README/SUBMISSION claim fixes, ADK-observability wording, disclosure note, runbook bonus-cap fix) | MED | S (0.5 d) | Week 1 (parallel) |
| 5 | **P3: demo-script v2** (hook, live FIX beat, BYOF line, Cloud Console shot, run-1→run-2 LEARN beat) + reassemble final video | MED-HIGH | S (1–2 d) | Week 2–3, after 1 & 2 |
| 6 | **Confidence-calibrated fallback (f)** — 0.5 d, folds into router + a test | MED | S | Week 2 |
| 7 | **Bonus points**: blog (+0.2, public, "created for purposes of entering this hackathon"), X/LinkedIn post (+0.2, #AllThingsAgenticHackathon) | MED (score floor) | S | Week 3 |
| 8 | ADK sub-agents in flagship (d) / minimal `/v1/events` dashboard (g) / Veo or Lyria integration (+0.4) | LOW–MED | M each | Only if time after 1–7 |
| 9 | Full auto-apply LEARN, full OTel observability, third canned workflow, UI rebuild | NO-GO | — | Skip — brand + time risk |

### Do this week: top 5
1. **Deploy** — `gcloud auth login` → fix `deploy/deploy.sh` (add `GEMINI_API_KEY` from secret) → `bash deploy/deploy.sh` → confirm `/health` and a live submit show `router: gemini-3.6-flash-live`. (Adam, ~30 min; everything else waits on this)
2. **Submit the credit form** (Aug 28 12:00 PT deadline; track **TASKMASTER**, 1–2 sentence pitch from runbook).
3. **Record GCP proof**: browser `.run.app` + Cloud Run dashboard + `python deploy/demo_probe.py <URL>`; commit `docs/GCP-PROOF.md`; assemble `docs/demo-video-final.mp4` (≤2:30).
4. **P1 build hop** — ADK `LlmAgent` + `FunctionTool` writes real `solution.py`; independent `self-test` node writes EVAL.json; gate verifies; offline fallback preserved; ADK test extends to the flagship path.
5. **Submission hygiene** — fix title/tests/LOC/claims/runbook; add disclosure note; update SUBMISSION.md checklist; re-run full suite.
