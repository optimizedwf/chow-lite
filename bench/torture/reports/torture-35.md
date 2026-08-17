# Torture-35 Report — workflows + router + CLI + docs (fresh angles)

Worker: torture-35 (round-18) — adversarial simulated user of the "nine" agent-OS.
Surfaces: workflow DAG edges / router / CLI / docs / truncation.
Hermetic only: no Gemini quota, no ADK model calls. All repros run with
`.venv/bin/python` and pure-python fixtures under /tmp.

## FINDING 1
- area: workflows (truncation / task-cap parity)
- severity: medium
- title: T29-F4 "all task+fix_dir slices routed through _cap_task_text" is
  incomplete — debug_wf diagnose/patch hops still head-truncate the task and
  hardcode `[:1500]` for fix_directive, dropping the tail (where acceptance
  criteria live) with NO truncation marker
- evidence: nine/workflows/debug_wf.py:3983-3990 (`task = str(inputs.get("task",""))[:_task_cap]`,
  `fix_dir = str(inputs.get("fix_directive", ""))[:1500]`) and :7998-8005
  (patch hop, same pair). flagship.py gained `_cap_task_text` (60% head / 40%
  tail + marker) in slice-50, but debug_wf still uses head-only `[:N]` slices
  and a hardcoded `[:1500]` for fix_dir (the exact anti-pattern T29-F4 was
  filed for). Repro: set NINE_TASK_CAP=50 and submit a debug task whose
  acceptance criteria live in the last 20 chars — the diagnose node sees only
  the head; the tail is silently gone.
- impact: debug-lane models diagnose/patch from a truncated task with no
  signal that content was cut; acceptance criteria in the tail are invisible
  -> wrong root-cause, extra FIX loops, wasted model budget. NINE_TASK_CAP is
  documented as THE knob but debug_wf ignores it for fix_directive (1500
  hardcoded).
- suggested_fix: route both debug_wf sites through flagship's
  `_cap_task_text` (move it to a shared module, e.g. nine/runtime/
  truncate.py, import in both), including fix_dir. Regression test:
  parametrize the existing test_t29_f4 over flagship AND debug_wf hops
  asserting the marker is present when len(text) > cap and that a
  tail-only acceptance phrase survives.
- effort: S
