# TORTURE-TESTER-19 Report — slice-35 round-9 fix-areas adversarial audit (torture-19)

Worker: TORTURE-TESTER-19. Tasked against HEAD `00cbc46` (slice 35 — "round-9 torture harvest (torture-17/18) — 16 findings fixed, 16 hermetic tests, 447 passed / 5 skipped"). Tree note: HEAD has since advanced to `7b14e2c` (slice 36 — fixture bugfix-small-010 + LEDGER backfill); `git diff 00cbc46 7b14e2c` touches none of the files cited below (only `bench/bench_nine.py` FIXTURES range 1→10, the new fixture, TRACKER/state/LEDGER). The uncommitted working-tree edits (README/SUBMISSION/LEDGER, deploy/server.py, nine/cli.py `_execute_job` route-event region, nine/gates/evidence.py, nine/registry.py) also avoid every cited line — re-verified at report time. All repros hermetic (no Gemini, no network, no quota): `.venv/bin/python` (3.12.13) scripts under /tmp, real modules + in-process fakes only. READ-ONLY: no repo file modified by this worker; the only repo write is this report.

Attack surfaces probed (13 fix-areas from the round-9 harvest, sampled): (1) T17-F1 TOCTOU re-hash + symlink indirection, (2) T17-F8 outside-artifact name collision, (3) T16-F4 redact() key-coverage breadth, (4) T17-F4/T15-F9 pid-file parse + recycled-pid identity gate, (5) T14-F5/T15-F10/T17-F5 constant snapshotting, (6) T18-F1 Firestore/JSONL ledger parity, (7) T16-F2/T17-F6 date-time strictness, (8) T16-F9 catalog commit contract, (9) server CANCELLED/route-event durability, (10) chain hop verdict recording, (11) `_catalog_is_committed` gitignore blind spot, (12) `_looks_like_job` route_decision shape, (13) ledger `add_verdict` terminal guard.

Verified-holding surfaces (not re-filed): server order-of-ops (learner/memory hoisted before `ledger.submit`; CANCELLED verdict skips `_record_route_event`); chain.py per-hop RouteEvent recording (verdict enum exactly matches gate-producible verdicts; CANCELLED hops early-return); `_catalog_is_committed` — catalog.json IS git-tracked and NOT ignored (`git ls-files` + `git check-ignore -v` verified), so the gitignore blind spot is not exploitable in this repo; "unregistered workflow id" 502-after-commit path — unreachable in-process (`Router.classify` validates the model `wf_id` against KEYWORDS ⊆ WORKFLOWS ∪ CHAINS); `_check_date_time` lowercase-`z` rejection — actually CORRECT (`str.replace("Z", ...)` is case-sensitive; Python 3.12 `fromisoformat` rejects lowercase z) — the docstring is right and the earlier suspicion is dropped; ledger `add_verdict` terminal guard (shipped/blocked/failed refuse non-CANCELLED verdicts; `cancelled` is the documented re-verdictable marker).

7 findings: 1 high, 5 medium, 1 low.

---

## FINDING 1
- area: evidence-gate TOCTOU hardening (torture-17 F1 + torture-17 F7 symlink blessing) — `nine/runtime/workflows.py`
- severity: high
- title: a check whose `.expected` names a symlink ("latest.md -> REPORT.md", the pattern torture-17 F7 explicitly blesses) skips the re-hash entirely — a late writer swapping the TARGET between registration and gate read SHIPs a manifest whose sha256 never matched the certified content (the T17-F1 hole re-opened by indirection)
- evidence:
  - `nine/runtime/workflows.py:713-737` — the per-check audit blesses a symlink at an expected path when `target_rel in registered` (734-735 `continue`) — NAME membership only, no content verification, per the torture-17 F7 comment ("the natural latest.md -> REPORT.md versioned pattern … its content is registered under the target's name").
  - `nine/runtime/workflows.py:818-835` — the torture-17 F1 TOCTOU re-hash (the ONLY content check between registration and SHIP) looks up `refs_by_name.get(expected_name)` (821). Symlinks are never registered (`_manifest_files` skips them, 158; the explicit branch skips them, 629), so for a symlink-named expected the ref is None → `continue` (822-823); lines 824-826 also `continue` on `mp.is_symlink()`. The re-hash therefore never touches the TARGET the gate actually certifies.
  - `nine/runtime/workflows.py:730-731` — same region has a second, opposite defect: `p_expected.resolve().relative_to(job_dir)` raises (target_rel=None → false BLOCK) whenever the workdir sits under a symlinked prefix — macOS `/var`→`/private/var` and `/tmp`→`/private/tmp` — so on macOS the blessed pattern is a permanent false BLOCK in temp dirs.
  - Repro `/tmp/t19_repro_symlink4.py` (real WorkflowExecutor, workdir under $HOME; node writes REPORT.md="A"*100 + symlink latest.md→REPORT.md; plugin check with `.expected=["latest.md"]` certifying the CONTENT at the link — the documented versioned pattern; monkeypatched `gate.evaluate` rewrites REPORT.md to "B"*100 AFTER registration, BEFORE the gate read): verdict **SHIP**, manifest `REPORT.md.sha256 == sha256("A"*100)` while disk == "B"*100 — the manifest lies about certified content. Control (same attack, check certifying "REPORT.md" directly): **BLOCK** "content changed during gate evaluation". Benign (no tamper): SHIP. Repro `/tmp/t19_repro_macos.py`: benign pattern under `/var/folders/...` → **BLOCK** "stale artifact(s): ['latest.md']" (the resolve() mismatch).
- impact: the round-9 T17-F1 fix ("re-hash every registered artifact named by a check's .expected and compare with the manifest sha256; mismatch = the manifest lies -> BLOCK") is silently inert for the exact symlink pattern the same slice blessed as safe. A concurrent/late writer (abandoned daemon thread from a timed-out callable node, nohup'd bash writer — the two threat models T17-F1 names) can swap the target between node-run and gate read; the job SHIPs with `nine artifacts`/evidence replay showing a hash that was never on disk at gate time. On macOS the same code path false-BLOCKs the benign pattern, breaking the documented workflow.
- suggested_fix: re-hash the RESOLVED target (and resolve the job dir consistently so the prefix-symlink case stops false-BLOCKing):
  ```python
  refs_by_name = {a["name"]: a for a in artifacts}
  resolved_dir = job_dir.resolve()      # /var->/private/var on macOS: resolve BOTH sides
  for _name, fn in self.gate.checks.items():
      for expected_name in (getattr(fn, "expected", None) or []):
          p_expected = resolved_dir / expected_name
          if not p_expected.exists():
              continue
          if p_expected.is_symlink():   # blessed latest.md -> REPORT.md: re-hash the TARGET
              try:
                  target_rel = p_expected.resolve().relative_to(resolved_dir).as_posix()
              except (ValueError, OSError):
                  continue
              ref = refs_by_name.get(target_rel)
              mp = p_expected.resolve()
          else:
              ref = refs_by_name.get(expected_name)
              mp = job_dir / expected_name
          if ref is None or not mp.is_file() or mp.is_symlink():
              continue
          try:
              if self._hash(mp.read_bytes()) != ref["sha256"]:
                  stale.append(f"{expected_name} (content changed during gate evaluation)")
          except OSError:
              stale.append(f"{expected_name} (unreadable during gate evaluation)")
  ```
  and change the audit's `relative_to(job_dir)` at 730-731 to `relative_to(job_dir.resolve())`. Regression test: executor + check `.expected=["latest.md"]` with the tampering gate wrapper must BLOCK (currently SHIPs); benign symlink pattern under a `/var`-symlinked prefix must SHIP (currently BLOCKs).
- effort: S

## FINDING 2
- area: outside-artifact namespacing (torture-17 F8) — `nine/runtime/workflows.py`
- severity: medium
- title: outside-artifact namespace is keyed on the parent NAME only — `/x/a/report.md` and `/y/a/report.md` both become `"../a/report.md"`, one file's evidence is silently REPLACED, and the surviving name resolves to a THIRD location that the re-hash can never verify
- evidence:
  - `nine/runtime/workflows.py:648-654` — the torture-17 F8 fix comment claims the outside namespace is "unique per (parent, basename)", but line 654 is `rel = "../" + p.parent.name + "/" + p.name` — keyed on `p.parent.name` only, so two DIFFERENT roots with the same immediate parent name collide.
  - `nine/runtime/workflows.py:592-597` — `_register`'s same-name REPLACE (`name in seen_idx` → `artifacts[idx] = artifact`) silently drops the first file's manifest entry.
  - `nine/runtime/workflows.py:824-826` — the T17-F1 re-hash resolves expected names via `job_dir / expected_name`; `job_dir/../a/report.md` is neither `/x/a/report.md` nor `/y/a/report.md`, so a gate certifying `../a/report.md` can never be content-verified (not a file → skip).
  - Repro `/tmp/t19_repro_nested.py` (two tool nodes returning `artifact_path` for `/tmp/.../x/a/report.md` and `/tmp/.../y/a/report.md`): manifest contains ONE entry, `name='../a/report.md'`, `produced_by=n2`, path=/y/a/report.md; the /x/a/report.md entry is gone (`X file registered? False`); `(job_dir / "../a/report.md").exists() == False`.
- impact: any tool/plugin certifying two outside files that share a parent name (nested under different project roots — the exact scenario F8's "x/report.md vs y/report.md" fix was meant to cover one level deeper) silently loses one file's evidence from the shipped manifest while keeping a name that neither `nine artifacts` nor the gate's own re-hash can resolve back to a real file — same evidence-integrity class as F1, plus a name-resolution lie.
- suggested_fix: make the outside namespace unique per resolved parent path:
  ```python
  # outside artifact: parent name + 8-hex digest of the resolved parent,
  # so /x/a/report.md and /y/a/report.md never collide
  rel = "../" + p.parent.name + "-" + self._hash(
      str(p.parent.resolve()).encode("utf-8"))[:8] + "/" + p.name
  ```
  Regression test: two `artifact_path` files with the same parent name under different roots → both manifest entries present with distinct names; each name resolves (job_dir / rel) to its real file.
- effort: S

## FINDING 3
- area: redact() key-coverage breadth (torture-16 F4 `*_key` expansion) — `nine/router/classifier.py`
- severity: medium
- title: the key-name alternations only cover underscore spellings — hyphenated (`private-key=`, `ssh-key=`), camelCase (`privateKey=`, `clientKey=`), `--flag=value` and urlencoded (`api_key%3D`) credential shapes are stored RAW through the submit redact boundary
- evidence:
  - `nine/router/classifier.py:59,64,65,67,68,70` — every key alternation lists `api[_-]?key|private_key|public_key|consumer_key|access_key|client_key|secret_key|ssh_key` — no `private-key`/`secret-key`/`client-key`/`ssh-key`/`access-key` hyphen forms and no `privateKey`/`clientKey`/`secretKey` camelCase forms. `re.IGNORECASE` does not help: the hyphen/underscore/case variants are distinct characters.
  - `nine/router/classifier.py:83` — pattern 10 (CLI flags) is `(--(?:...)[_-]key)\s+\S+`: whitespace-separated only — `--private-key=hunter2` (equals form) is unmatched.
  - `nine/router/classifier.py:67` — plain `[=:]` pattern cannot match the urlencoded separator `api_key%3Dsupersecret`.
  - The boundary IS the durable-store gate: `nine/ledger/ledger.py:263-267` and `nine/ledger/firestore_ledger.py:46-50` redact the task at submit on BOTH backends; the round-9 `*_key` expansion (T16-F4 comment "API_KEY=, PASSWORD= … leak verbatim today") shows the intent to cover key spellings.
  - Repro `/tmp/t19_repro_redact_e2e.py` (submit through JSONLLedger): task `"Deploy with --private-key=hunter2 and \"clientKey\": \"abc123\", ssh-key xyz, then api_key%3Dsupersecret and GitHub token ghp_..."` → stored ledger input contains ALL of `--private-key=hunter2`, `"clientKey": "abc123"`, `ssh-key xyz`, `api_key%3Dsupersecret` verbatim (only the `ghp_` token was caught). Unit repro `/tmp/t19_repro_redact2.py`: `"private-key": "hunter2"`, `privateKey=ghp_abcdefgh123456`, `--private-key=hunter2` all pass through `redact()` unchanged, while the underscore forms redact.
- impact: the most common real-world credential spellings in pasted tasks (`--private-key=`, `"clientKey":`, `ssh-key `) reach the durable ledger, Firestore (production), route events and `nine jobs` output in plaintext — exactly the leakage class T4-F4/T16-F4 was built to stop, still open for the hyphen/camelCase/flag-equals variants.
- suggested_fix: extend the shared alternation with hyphen + camelCase forms and teach pattern 10 the `=` form (one alternation constant reused by all patterns):
  ```python
  _KEY = (r"(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|"
          r"public[_-]?key|consumer[_-]?key|access[_-]?key|client[_-]?key|"
          r"secret[_-]?key|ssh[_-]?key|privateKey|secretKey|clientKey|"
          r"accessKey|consumerKey|sshKey|aws_secret_access_key|aws_access_key_id)")
  # pattern 10 becomes:
  (rf"(--{_KEY})\s*[= ]\s*\S+", r"\1 ***", re.DOTALL | re.IGNORECASE),
  # plus a urlencoded-separator pattern: (api[_-]?key|...)(?:%3D|=)...
  ```
  Regression test: the 8 leaked strings from `/tmp/t19_repro_redact2.py` (hyphenated `=`, quoted hyphenated, camelCase, `--flag=value`, non-prefixed values) must redact.
- effort: S

## FINDING 4
- area: bench pid-file parsing / recycled-pid identity gate (torture-15 F9, torture-17 F3/F4) — `bench/bench_nine.py`
- severity: medium
- title: a pid-file line with second field `nan` passes `float()` and then makes `abs(actual - start) > 1.0` always-False — the session-leader + spawn-epoch identity gate is bypassed and an arbitrary session-leader process group gets SIGTERM/SIGKILL
- evidence:
  - `bench/bench_nine.py:486-494` — `start = float(parts[1]) if len(parts) > 1 else None` (487): `float("nan")` SUCCEEDS (only `ValueError` is caught); the torture-17 F4 comment (489-492) threat-models "node-controlled pid file" but `nan` is not rejected.
  - `bench/bench_nine.py:516` — `if actual is None or abs(actual - start) > 1.0: continue` — `abs(real - nan)` is `nan` and `nan > 1.0` is False, so the line never continues; gate 2 passes and `os.killpg(pid, SIGTERM)` (518) + SIGKILL (523) fire. (`inf` is safe: `abs(...) > 1.0` is True; non-numeric garbage is caught by ValueError.)
  - Reachability: `.nine-node-pids` lives in the job dir, writable by the runtime's own bash nodes (`nine/runtime/workflows.py:179-180` append loop; the F4 comment itself names the node-controlled threat model). A node writing `<pid-of-any-session-leader> nan` to the pid file (or a torn write producing `nan`) kills that innocent group when the bench sweeper runs after a fixture timeout.
  - Repro `/tmp/t19_repro_nan.py` (exact parse+gate code): line `"999999 nan"` with `actual_epoch=1700000000.0` → identity gate **PASSES (KILL!)**; `inf` and `"1234"` are rejected; `float("nan")` parses without error.
- impact: the T15-F9/T17-F3 guarantee ("a recycled pid must never SIGKILL an innocent process group") is one `nan` away from being void; on the bench host, an adversarial or crashed node can make the cleanup sweep terminate an unrelated session-leader process group (the CI host's own daemons, other fixtures' node groups).
- suggested_fix: reject non-finite parses alongside the ValueError branch (2 lines):
  ```python
  import math  # top of file
  ...
  try:
      start = float(parts[1]) if len(parts) > 1 else None
      if start is not None and not math.isfinite(start):
          continue  # nan/inf can never match a real spawn epoch
  except ValueError:
      continue
  ```
  Regression test: `_kill_node_groups` over a pid file containing `<pid> nan` (with the pid a live session leader) must NOT kill.
- effort: S

## FINDING 5
- area: bench runner-to-pytest conversion constants (torture-14 F5, torture-15 F10, torture-17 F5) — `bench/bench_nine.py`
- severity: medium
- title: `_constant_snapshots` keeps a stale literal when a constant is REASSIGNED to a non-literal expression — the converted pytest test asserts a contract that never existed (false red on correct code / false green on broken code)
- evidence:
  - `bench/bench_nine.py:166-187` — the snapshot loop handles `Assign` with Constant RHS (167-170), `AugAssign` with Constant RHS (171-183) and `Delete` (184-187); an `Assign`/`AugAssign` whose RHS is NOT a Constant matches NO branch, so the previous literal stays in `consts` and is snapshotted into the next `test()` call at line 192.
  - `bench/bench_nine.py:304`-region `_InlineDangling` — the stale value is then inlined into the converted pytest source as the assertion target.
  - Repro `/tmp/t19const/run.py` (real `convert_to_pytest`): runner `EXPECTED = 5; EXPECTED = add(1, 2); test("case1", add(1, 2), EXPECTED)` converts to `assert add(1, 2) == 5`; executing the converted test with `add = a+b` FAILS (3 != 5) — a false red on correct code, while the original runner passes (3 == 3). Mirror direction: `EXPECTED = add(1,2)` first, then a broken reassignment → false green.
- impact: bench fixtures that compute an expected value dynamically (the T15-F10 reassignment pattern, one non-literal step away from what the code already supports) get a converted suite that asserts a phantom contract; the fix loop chases a failure that never existed in the original runner (false red), or certifies broken code (false green).
- suggested_fix: drop the name when its binding is no longer a literal (mirror the Delete branch, ~6 lines):
  ```python
  elif isinstance(node, ast.Assign) and not isinstance(node.value, ast.Constant):
      for t in node.targets:
          if isinstance(t, ast.Name):
              consts.pop(t.id, None)
  elif isinstance(node, ast.AugAssign) and not isinstance(node.value, ast.Constant):
      consts.pop(node.target.id, None)
  ```
  (A non-Constant binding leaves the name dangling → `_InlineDangling` raises loudly instead of asserting the stale literal.) Regression test: the `/tmp/t19const/run.py` runner must NOT convert to `== 5` (loud error or name left un-inlined).
- effort: S

## FINDING 6
- area: Firestore/JSONL ledger contract parity (torture-18 F1) — `nine/ledger/firestore_ledger.py` + `nine/cli.py`
- severity: medium
- title: FirestoreLedger (the production Cloud Run backend) silently no-ops `recover()` on non-blocked/failed jobs and lacks `refresh()`/`_jobs` — `nine recover <shipped-job>` wipes verified artifacts and re-executes, and `nine recover --force` raw-crashes with AttributeError in production
- evidence:
  - `nine/ledger/firestore_ledger.py:107-112` — `recover()` returns the job UNCHANGED when status is not blocked/failed; `nine/ledger/ledger.py:360-383` — `JSONLLedger.recover` raises `LedgerError("only blocked/failed can be recovered")`. The class has NO `refresh()` method and NO `_jobs` dict (full class read, 122 lines).
  - `nine/cli.py:541-551` — `cmd_recover` calls `ledger.recover(...)`, then unconditionally wipes the job dir (546-551) and re-executes (555) — it never re-checks `job.status`, trusting the backend to have refused.
  - `nine/cli.py:519,539` — the `--force` branch calls `ledger.refresh(args.job_id)` and `ledger._jobs[args.job_id] = live` — both missing on FirestoreLedger → AttributeError raw traceback.
  - `deploy/server.py:188-204` — `get_ledger()` prefers Firestore whenever `google.cloud.firestore` imports: Firestore IS the production backend.
  - Repro `/tmp/t19_repro_fs.py` (logic repro via `object.__new__`, no google-cloud needed): JSONLLedger.recover on a `submitted` job → `LedgerError: job ... is submitted, only blocked/failed can be recovered`; FirestoreLedger.recover on the same job → returns the job with `status: submitted` (silent no-op, no error).
- impact: on the deployed backend, a mistaken `nine recover` on a shipped job destroys its verified artifacts (job-dir wipe) and re-runs it (transition crash or double-execution) with no error; `nine recover --force` (the documented stale-running recovery, T8-F6) is completely broken in production. The "API mirrors JSONLLedger so swapping backends is a one-line change" docstring claim (firestore_ledger.py:9) is false for the methods cli.py depends on.
- suggested_fix: mirror the JSONL contract (~12 lines):
  ```python
  def refresh(self, job_id: str) -> Job:
      """Firestore reads are always durable — same contract as JSONLLedger.refresh."""
      return self.get(job_id)

  def recover(self, job_id: str) -> Job:
      job = self.get(job_id)
      if job.status not in ("blocked", "failed"):
          raise LedgerError(
              f"job {job_id} is {job.status}, only blocked/failed can be recovered")
      job.transition("recovered")
      job.attempts = 0
      self._ref(job_id).update({"status": job.status, "updated_at": job.updated_at,
                                "attempts": job.attempts})
      return job
  ```
  plus `self._jobs: dict[str, Job] = {}` in `__init__` (populated by `get()`/`refresh()`) so cli.py:539 stops AttributeErroring. Regression test: fake Firestore client — `recover()` on a submitted/shipped job raises LedgerError; `refresh()` returns the durable doc; `nine recover --force` path does not raise AttributeError.
- effort: S

## FINDING 7
- area: date-time format strictness (torture-16 F2, torture-17 F6) — `nine/schema_validation.py`
- severity: low
- title: `_check_date_time` accepts non-RFC-3339 offsets — `+00`, `-00`, `+01` (hour-only) and `+0530` (colon-less) pass, though the checker claims an "RFC 3339 subset" (RFC 3339 time-offset requires Z or `+HH:MM`)
- evidence:
  - `nine/schema_validation.py:59-75` — `_check_date_time`; line 72 `_dt.datetime.fromisoformat(value.replace("Z", "+00:00"))`. Python 3.12 `fromisoformat` accepts hour-only offsets (`+00`) and colon-less offsets (`+0530`); the docstring (53-54) claims "RFC 3339 subset".
  - Repro `/tmp/t19_repro_dt2.py` (venv 3.12.13): `2026-08-13T12:00:00+00`, `...-00`, `...+01`, `...+0530` all accepted by `is_valid("agent-job", ...)`; lowercase `z` is correctly REJECTED (`replace("Z", ...)` is case-sensitive and `fromisoformat` rejects `z`) — so only the offset strictness is off.
- impact: malformed timestamps from a hand-edited job record or plugin verdict pass the declared boundary and enter durable stores/analytics with a non-canonical offset shape; low impact (comparisons still work — offsets are aware), but the checker's documented contract is wider than RFC 3339.
- suggested_fix: after the fromisoformat parse, reject offsets that are not `Z` or `±HH:MM` (~5 lines):
  ```python
  import re as _re
  ...
  m = _re.search(r"[+-]\d\d(?::?\d\d)?$", value)
  if m and len(m.group(0)) != 5:   # +00 / +0530: not RFC 3339 (+HH:MM or Z only)
      return False
  ```
  Regression test: `+00`, `-00`, `+01`, `+0530` rejected; `Z`, `+00:00`, `+05:30` accepted.
- effort: S
