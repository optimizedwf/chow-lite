# TORTURE-24 — Runtime + Gates (slice-43 edges), round 12

**Repo**: chow-lite · **Round**: 12 (torture-24) · **Surface**: adk_runtime
retry/empty-stream/RunConfig, EVAL gate parsing + NINE_GATE_TIMEOUT_S
daemon-thread gate, self-test/verify workflow, ledger JSONL/Firestore,
node timeouts, manifest/artifact handling, fsafety contained_write,
NINE_MAX_LLM_CALLS fallback, FIFO/device guards, Firestore malformed-doc,
verify_wf is_file() guards.

**Method**: read-only static exploration + hermetic repros
(`.venv/bin/python`), zero Gemini quota used, no real model calls, no git
touches. No repo file modified except this report.

**Baseline**: slice-43 shipped T21-F1..F6 + T22-F1..F3 (FIFO/hang gates,
gate daemon-thread timeout, redaction families, aux-write best-effort,
timeout env validation, CLI OSError belt, Firestore malformed-doc). This
round hunts NEW gaps at the edges of those fixes.

Findings: 5 (4 medium, 1 low).

---

## FINDING 1
- area: workflows / verify
- severity: medium
- title: verify lane's mechanical-check bash node still BLOCKS on a FIFO at EVAL.json — the slice-43 FIFO guard never reached `_check_command`'s heredoc (`ej.exists()` -> `read_text()`), a 300s node-timeout hang per attempt
- evidence: nine/workflows/verify_wf.py:224-240 (`ej = Path("EVAL.json"); if ej.exists(): d = json.loads(ej.read_text(...))`) — `exists()` is True for a FIFO, and `read_text()` on a FIFO blocks until a writer appears. slice-43 (T21-F1) guarded `load_eval_json` (evidence.py:87 `p.is_file()`) and the two Python gate checks (`_verified_json_check`/`_honesty_check`, verify_wf.py:281/321 `is_file()`), but the lane's own `check` bash node reads EVAL.json unguarded. Repro (hermetic):
  ```bash
  cd /tmp && rm -rf vf && mkdir vf && cd vf
  mkfifo EVAL.json
  timeout 3 python -c "from pathlib import Path; Path('EVAL.json').read_text()"; echo "rc=$?"
  ```
  -> rc=124 (blocked 3s; in nine the node burns the full 300s default `timeout_seconds` before TimeoutExpired -> group kill -> job FAILS). Same vector: a claim referencing a FIFO `x.py` runs `py_compile` on it (verify_wf.py:190) which also blocks until the bash-node timeout.
- impact: a task that `mkfifo EVAL.json` (or any FIFO claim ref) DoSes the verify lane for the full node timeout on every attempt — the exact unbounded-read family T21-F1 closed for the gate is still open on the lane's own inventory/check node; a FIFO in a chain's job dir also stalls hop 3 (verify) of every flagship run.
- suggested_fix: in `_check_command`'s heredoc use `ej.is_file() and not ej.is_symlink()` before `read_text()` (mirror load_eval_json); same for `cm.exists()` and any claim-ref file before stat/py_compile (skip non-regular refs like the gate does). Regression test: `mkfifo EVAL.json` + run the check node command -> CHECKS.json written within ~1s with EVAL.json treated as absent, no block.
- effort: S

## FINDING 2
- area: runtime / ledger
- severity: medium
- title: FirestoreLedger shape guard (T21-F6) only checks identity fields — a wrong-typed `created_at`/`status` doc still raw-TypeErrors `discover()`/`stats()` (HTTP 500)
- evidence: nine/ledger/firestore_ledger.py:60-74 `_job_from_rec` validates `workflow_id`/`job_id` strings only, then blindly `job.__dict__.update(rec items)`. `discover()` sorts by `j.created_at` (line 89): a doc with `created_at: null` or `created_at: 12345` makes `sorted()` raise `TypeError: '<' not supported between instances of 'NoneType' and 'str'`. `stats()` (line 118) buckets `doc.to_dict().get("status")` — a doc with `status: {"x":1}` raises `TypeError: unhashable type: 'dict'`. Both raw-500 on Cloud Run. Repro (hermetic, no firestore client needed — `_job_from_rec` is static):
  ```python
  from nine.ledger.firestore_ledger import FirestoreLedger
  from nine.ledger.ledger import Job
  a = FirestoreLedger._job_from_rec({"workflow_id":"w","job_id":"a","created_at":None})
  b = FirestoreLedger._job_from_rec({"workflow_id":"w","job_id":"b","created_at":"2026-08-15T00:00:00+00:00"})
  sorted([a, b], key=lambda j: j.created_at, reverse=True)  # TypeError
  ```
- impact: Firestore docs are console-editable/version-driftable — one bad field kills `/v1/jobs`/`/v1/stats` for every caller, the exact JSONL-parity goal of T21-F6 (JSONLLedger's `_looks_like_job` validates status/artifacts/verdicts/attempts types, Firestore's does not).
- suggested_fix: `_job_from_rec` should type-check the fields the API consumes (created_at ISO-str, status str, artifacts/verdicts list, attempts int) and return None (skip) on mismatch — mirror JSONLLedger._looks_like_job; stats() should stringify/skip unhashable status values. Regression test: two malformed docs (created_at None; status dict) -> discover/stats return healthy docs without raising.
- effort: S


---

## FINDING 3
- area: runtime (fsafety contained_write)
- severity: medium
- title: contained_write has NO write-side FIFO guard — a pre-existing FIFO at a known write target (solution.py / EVAL.json / HANDOFF.md) makes the model's write_file tool call block until the node timeout (slice-43 guarded reads; the write side still hangs)
- evidence: nine/runtime/fsafety.py:31-47 — `target.write_text(content)` on a path that is a FIFO opens O_WRONLY and blocks until a reader appears; nothing checks `target.is_file()`. Repro (hermetic):
  ```bash
  mkfifo solution.py
  timeout 3 python -c "from nine.runtime.fsafety import contained_write; print(contained_write(Path('.'), 'solution.py', 'x=1'))"
  # rc=124 (blocked 3s; in nine the ADK tool call burns the full node timeout_seconds)
  ```
  In nine the hang happens inside ADKAgentNode's tool loop -> callable-node deadline abandons the daemon thread -> NodeTimeoutError -> job FAILS (slice-44's `{stripped}` debug loop and T21-F1's FIFO family all over again, but on the WRITE path, which T21-F1 never touched). The bench's seeded test_solution.py and every flagship hop's write targets are predictable names.
- impact: a task or earlier bash hop can `mkfifo solution.py` and stall every build/debug hop for the full node timeout, spawn an abandoned daemon thread per attempt, and turn a fixable job into a FAILED one — same DoS family as the read-side FIFO fix, still open on writes.
- suggested_fix: in contained_write, before mkdir/write, if the resolved target exists and is not a regular file (or is a symlink), raise a clean ValueError naming the type; regression test: `mkfifo solution.py` -> contained_write raises within ~1s with no block, and the tool error surfaces to the model instead of hanging.
- effort: S

## FINDING 4
- area: gates / chains
- severity: medium
- title: flagship review hop's consistency gate still reads EVAL.json with exists() — a FIFO at EVAL.json converts T21-F1's instant-FAIL into a 60s daemon-thread gate timeout per hop attempt (BLOCK + abandoned thread each retry)
- evidence: nine/chains/flagship.py:403-405 `_review_verdict_consistent`: `if not rp.exists() or not ep.exists(): return False...` then `json.loads(ep.read_text(...))` — no is_file() guard (the T21-F1 FIFO guard was applied to load_eval_json in evidence.py:87 and verify_wf's two checks, but NOT to this flagship gate check). Repro (hermetic): `mkfifo EVAL.json` then `json.loads(Path('EVAL.json').read_text())` -> blocks (rc=124). Inside the gate it is bounded only by NINE_GATE_TIMEOUT_S (default 60s, workflows.py `_run_gate`), so each gate evaluation stalls 60s, returns BLOCK, the chain FIX-retries the hop (chain.py: any non-SHIP verdict retries while attempts remain), and each retry spawns a NEW daemon thread stuck on the FIFO read.
- impact: a flagship chain whose workspace contains a FIFO at EVAL.json (created by an earlier hop's bash node) stalls for minutes (3 attempts x 60s+) and ends BLOCKed with a misleading 'gate timed out' summary instead of the honest 'EVAL.json missing/not a regular file' FAIL; abandoned gate threads accumulate per retry.
- suggested_fix: use `ep.is_file() and not ep.is_symlink()` (mirror load_eval_json) before read_text in `_review_verdict_consistent` (and `rp` for review.md); sweep every remaining `exists()`+read site inside registered gate checks. Regression test: FIFO at EVAL.json -> check returns False immediately (no timeout needed).
- effort: S

## FINDING 5
- area: runtime / docs (env-var consistency)
- severity: low
- title: NINE_MAX_LLM_CALLS non-numeric value falls back to 24 SILENTLY while 0/negative warns loudly — the slice-43 range fix left the parse-failure path unobservable (typographic NINE_MAX_LLM_CALLS=abc is invisible to the operator)
- evidence: nine/runtime/adk_runtime.py:177-186 — `except ValueError: _max_calls = 24` (no stderr warning), while the `< 1` branch below prints "WARNING: NINE_MAX_LLM_CALLS must be >= 1 ...". Contrast the established convention from T9-F6 (junk NINE_LLM_BACKEND warns loudly once) and T22-F2/NINE_NODE_TIMEOUT_S handling: garbage env values are always surfaced. Repro: `NINE_MAX_LLM_CALLS=abc` -> int() ValueError -> 24 with zero output on stderr.
- impact: an operator who fat-fingers NINE_MAX_LLM_CALLS (e.g. `l4`, `24 ` with a stray char) silently gets the default budget — the cap they believed they tightened never applies, and runaway tool loops on a small local model re-burn budget invisibly.
- suggested_fix: mirror the `< 1` branch — on ValueError print the same one-line stderr WARNING (got value, using 24); regression test: run ADKAgentNode.__call__ path with NINE_MAX_LLM_CALLS=abc (hermetic fake runner) and assert the warning line appears.
- effort: S
