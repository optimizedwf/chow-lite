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

Findings so far: 2 (1 medium, 1 medium). More below as found.

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
