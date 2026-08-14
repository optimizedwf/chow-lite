# nine — Devpost submission (All Things Agentic Hackathon, id 30845)

Submission URL: https://devpost.com/submit-to/30845-all-things-agentic-hackathon/manage/submissions
Deadline: 2026-08-31 @ 5:00pm PDT

---

## Title
nine — the agent OS that proves its work

## Tagline (one line)
Every task routed, executed, verified against deterministic tests, and learned from — an agent that can't ship a lie.

## Project URL / Code Repo
https://github.com/optimizedwf/nine

## Video URL
(TBD — demo video: required. Candidate: demo_capture/v3_arch.png as diagram; need screen recording of a fixture run.)

## Architecture diagram
- repo: docs/architecture.svg + docs/architecture.png
- demo_capture/v3_arch.png (in-repo)

## Built With
Python, Google ADK 2.0, Gemini 3.6 Flash, deepseek-v4-flash, pytest, ruff, mypy, JSONL + Firestore (ledger), MCP

## About the project

### Inspiration
The first time I watched an agent confidently ship a wrong answer, I realized the problem wasn't the model — it was the pipeline. Everything upstream of the final answer was vibes: the agent produced text, we read it, we hoped. No checksums, no tests, no ledger of what it had tried or learned. And because the loop never closed, the same failure happened again the next day.

I wanted an agent that could prove its output the way a test suite proves code: deterministically, hermetically, and cheaply. Not "I think this is right" — "here is the evidence, and the gate agrees."

### What it does
nine is an evidence-gated agent OS on Google's ADK 2.0. Every task flows through four stages:

- ROUTE — a workflow registry routes the task to the right workflow (debug, build, …)
- EXECUTE — an LLM agent does the work (Gemini 3.6 Flash by default — but the model is a plug-in; the identical pipeline ran end-to-end on deepseek-v4-flash through a tunnel)
- VERIFY — hermetic, deterministic evidence gates judge the result: real tests run, artifacts are checksummed, provenance checked, secrets redacted. The verdict is SHIP, FIX, or BLOCK — never a silent pass
- LEARN — every failure is written to a durable learning ledger (JSONL + Firestore mirror) with recovery and redaction, so the next run starts smarter

The verdict is a formal predicate:

$$\mathrm{verdict}(f_i) = \begin{cases} \text{SHIP} & \text{if } \forall\, t\in \mathcal{T}_i:\ t(\hat f_i) = \text{pass} \\[2pt] \text{FIX} & \text{if } \exists\, t\in \mathcal{T}_i:\ t(\hat f_i) = \text{fail} \text{ and } \hat f_i \ne f_i^{(0)} \\[2pt] \text{BLOCK} & \text{otherwise} \end{cases}$$

A SHIP means every test in the fixture's suite passes on the agent's actual patch, verified against tamper-evident `.expected` artifacts — and a candidate that left the starter code unchanged can never SHIP. On our benchmark of 10 real bug-fix fixtures, nine ships 10/10 fixtures, 94/94 tests — real patches, not placeholders.

### How we built it
The system torture-tests itself. We ran 20+ adversarial torture rounds — simulated users hammering the system and filing sharp findings — and every round produced real fixes that became new hermetic tests. The suite now has 461 passing tests, and each one pins a real failure the system once had. That loop is the product: an agent OS that improves because its failures are measured, not anecdotal.

The bench itself is reproducible and quota-aware: it runs as a tagged experiment (run-id, backend, fixture subset), records quota state, and snapshots results — so "did it get better" is a query, not a memory.

### Challenges we ran into
- Rate limits look like nothing. ADK swallows Gemini free-tier 429s into empty streams — the agent appears to have "done nothing," with zero errors. Every silent failure became a loud one, and empty streams now retry with backoff before failing: $$t_k = b \cdot k,\quad b = 3\,\text{s} \quad\Longrightarrow\quad 3\,\text{s},\ 6\,\text{s},\ \text{then fail loud.}$$
- A mid-bench quota cliff: the canonical Gemini run SHIPped 5/10 fixtures before the day's budget vanished — invisibly. Diagnosing it meant correlating timing, attempt counts, and response patterns across fixtures. The fix is armored with hermetic tests; the remaining fixtures are queued for the next quota window.
- Verification is a rabbit hole of edge cases: symlink resolution differs across macOS paths (/var → /private/var), timestamps must be RFC 3339 with proper offsets, process sweeps must reject NaN epochs, secret-bearing keys must be redacted in every spelling (KEY, _key, --key=, URL-encoded). Each one became a hermetic test.
- Evidential integrity: making sure a SHIP can't be faked, a tamper can't pass, and a learning record can't be lost. That's a thousand small decisions — and each one is a test.

### Accomplishments that we're proud of
- 10/10 fixtures, 94/94 tests SHIPped with real patches (two different LLM backends)
- 461 hermetic tests; ruff + mypy clean
- Fail-loud on quota instead of silently shipping garbage — a structural guarantee, not a policy
- Model-agnostic: swapped the entire brain with an env var, same evidence, same gates, same score

### What we learned
- Fail loud beats fail silent. The worst bug in an agent OS is a fabricated SHIP; the second worst is a silent pass. Both are now structurally impossible.
- Model-agnostic pays off: when the primary backend hit a hard quota wall mid-bench, we swapped models and got the full 10/10 run — same evidence, same gates, same score.
- Verification is an endless edge-case minefield — and each edge case is a lesson worth keeping as a test.

### What's next for nine
More fixture specs, more workflows (build/learn/audit), and the official submission bench. The model gets plugged in at the end — because the OS is the point, and the brain is a component.

## Additional fields (fill in the form)
- Team: (name)
- Members: (add)
- Track: Fortified Enterprise Fleet (multi-agent) — confirm at submission


---

## ✅ Requirements audit (from official hackathon page + rules, fetched read-only)

Hackathon: **All Things Agentic Hackathon** — https://allthingsagentichackathon.devpost.com/
Deadline: **2026-08-31 @ 5:00pm PDT** (11:59pm ET)
Prizes: $180,000 total; Grand Prize $50k; winners per track (Taskmaster / Collaborative Partner / Fortified Enterprise Fleet)
Judging: Innovation & Operational Utility 40% · Architectural Discipline & Tech Stack 30% · Demo & Production Readiness 30% · Stage-Three bonus points (max +0.6)

### Mandatory technology (Stage One pass/fail)
- [x] Gemini 3.5+ via Gemini API or Vertex AI — `gemini-3.6-flash` used in bench runs
- [x] At least one Google agent framework — **Google ADK 2.0** (core runtime)
- [x] At least one Google Cloud infrastructure service — **Firestore** (learning ledger mirror)
- [ ] (verify) Firestore project/deployment visible; Google Cloud proof in video + repo

### What to Submit checklist
- [x] Text description (features, technologies, other data sources, findings & learnings) — written below
- [ ] URL to hosted project (encouraged; e.g. web UI) — nine is CLI/daemon; hosted URL optional but "highly encouraged"
- [x] URL to code repo (public or private; if private share with testing@devpost.com + cloudhackathons@google.com) — github.com/optimizedwf/nine (public)
- [ ] Spin-up Instructions in README.md (step-by-step run/deploy guide) — verify README has this
- [x] Architecture Diagram (Gemini↔backend↔db↔frontend) — docs/architecture.svg + .png
- [ ] **~4-min demo video** — NOT DONE (required; must show live unedited run + Google Cloud proof)
- [ ] **Public blog/podcast/video about the build** (must state it's for this hackathon) — NOT DONE (+0.2)
- [ ] **Public social media post** with #AllThingsAgenticHackathon on X/LinkedIn — NOT DONE (+0.2)
- [ ] **Additional Google AI model integration** (Gemma/Veo/Lyria) — NOT DONE (+0.2 each, max +0.6)

### Track fit — **TASKMASTER** (chosen in repo's SUBMISSION.md): "Bring Your Own Friction" — nine eats its own dogfood; the agent that built this repo used nine-style discipline. Track names from homepage: The Taskmaster, The Collaborative Partner, The Fortified Enterprise Fleet. (Judging bullets also mention Continuous Action Engine / Evolving Knowledge Engine / Multi-Agent Nexus — verify exact list at submission.)

### Status vs. gaps (repo already has: SUBMISSION.md, demo-script.md, ADAM-RUNBOOK.md, docs/architecture.svg+png, demo-video-v3.mp4 119.9s, deploy/, Dockerfile, README quickstart + required-tech table — all committed, tree clean at 63b1698)

DONE in repo:
- [x] Title/tagline/description (SUBMISSION.md §1–3, pastable)
- [x] Architecture diagram (docs/architecture.svg + .png)
- [x] Repo URL (github.com/optimizedwf/nine, public, MIT)
- [x] README spin-up instructions + required-tech compliance table
- [x] Demo video draft v3 (docs/demo-video-v3.mp4, 119.9s) — GCP segment slot pending
- [x] Required tech: Gemini 3.6 Flash ✓ ADK 2.0 ✓ Cloud Run + Firestore ✓

REMAINING (all Adam-only, ~30 min total, per docs/ADAM-RUNBOOK.md):
1. **Devpost registration** — sign in as optimizedworkflowdev, Join hackathon id 30845, pick TASKMASTER. (No Devpost session found in any local browser profile — needs user login.)
2. **GCP credits form** (deadline Aug 28 12:00 PT) — forms.gle/5PtXmw1dSbDnpYke9, TASKMASTER + pitch provided.
3. **GCP deploy + auth** — gcloud already installed; auth + deploy/deploy.sh → .run.app URL → then I record the live-GCP segment, assemble demo-video-final.mp4, upload public YouTube, and fill the Devpost form.

BONUS (+0.2 each, max +1.0 total per rules: 0.2 content + 0.2 social + 0.2×3 models capped 0.6):
- [ ] Public blog/video about build (with hackathon-disclosure line)
- [ ] Public social post with #AllThingsAgenticHackathon
- [ ] Optional second Google AI model (Gemma/Veo/Lyria) — Gemma teach hop candidate
