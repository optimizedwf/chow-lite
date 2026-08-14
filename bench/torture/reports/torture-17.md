# TORTURE-TESTER-17 Report — round 8 (respawn): holes in the slice-34 fixes (T15-F1/F3/F5/F9/F10/F11 + T16-F2 + stale-guard family)

Worker: TORTURE-TESTER-17 (round 8, respawn). Repo HEAD: 0c92623 (slice 34 — "round-8 torture
harvest (torture-15/16), 22 findings fixed"). All repros hermetic (no Gemini, no network, no
quota): `.venv/bin/python` scripts under `/tmp/t17/` (repros.py — R1..R10, r8c.py, runner
files), real modules + in-process stubs only. READ-ONLY: no repo file was modified; the only
repo write is this report (`git status` = pre-existing `M bench/state.json`).

Method: re-attacked each slice-34 fix at its edge, exactly as round-8 torture is supposed to.
Every finding below is a HOLE IN A SLICE-34 FIX, reproduced against HEAD: the fix works for
its test but not for the shape next to it. Prior reports (torture-15/16) read and deduped
against; where a finding extends a fixed finding the original id is cited.

Verified-holding surfaces (not re-filed): symlink-at-expected BLOCKs for OUTSIDE targets
(T15-F1 core — R4 control: outside-target symlink correctly BLOCKs); explicit artifact_path
honors ignore lists (T15-F3); outside artifact namespaced `../<name>` vs an INSIDE same-name
file (T15-F5 core — no collision with an inside file); pid-file two-field identity gate works
for a recycled pid whose epoch differs by >3s (T15-F9 core); per-call-site constant snapshots
work for plain reassignment `EXPECTED = 6` between calls (T15-F10 core); non-literal name args
slug instead of crash (T15-F11); `verified_at` fallback in the stale-BLOCK record fills a
missing value (T16-F2 adjacent).

---

## FINDING 1
- area: stale-artifact guard — name-only provenance, no content cross-check at ship time (`nine/runtime/workflows.py`)
- severity: medium
- title: TOCTOU: a registered file swapped between the manifest scan and the gate read SHIPs with a manifest sha256 that does not match the certified content
- evidence: `nine/runtime/workflows.py:616-652` registers artifacts by hashing DISK at node-run time; the stale guard at `:684-740` then audits only NAMES (`expected_name in registered` at `:740`) — it never re-reads the certified file, so the manifest hash can diverge from what the gate certified seconds later. The code itself documents the writer: `:479-487` records `timeout_abandoned_worker` — "a timed-out callable node leaves an abandoned daemon thread that may still write files" (torture-6 F5). Repro `/tmp/t17/repros.py` R2: node writes EVAL.json with a FAILING check; a `swap` check registered first rewrites EVAL.json to PASSING before `eval_json_check` runs; gate SHIPs; manifest entry sha256 = failing content, disk at ship time = passing content: `manifest sha256: f53f163c… | disk sha256 at ship: ae265a0e… | match: False`. No symlink, no stale name, no exemption involved — the guard's invariant "the manifest is what the gate certified" is false under any concurrent/late writer (abandoned thread, `nohup`'d writer from a bash node).
- impact: the durable manifest (ledger `artifacts`, `nine artifacts`, memory lineage) records a hash for content the gate never evaluated; replay/audit certifies the wrong bytes. The t7-F1/t10-F2 family exists precisely so a SHIP's certified content is in the manifest — this is the same lie, via timing instead of staleness.
- suggested_fix: in the SHIP branch, after `gate.evaluate`, re-hash every registered artifact named by a PASSING check's `.expected` (and the artifact refs) and compare with the manifest sha256; on mismatch append to `stale` (BLOCK). Cheap (a handful of files per attempt), no API change. Regression test: R2 shape — swap check + `eval_json_check` → expect BLOCK with a "content changed during gate evaluation" summary (today: SHIP with mismatched hash).
- effort: S

## FINDING 2
- area: stale-artifact guard — `.expected` provenance is opt-in (`nine/runtime/workflows.py`, `nine/gates/evidence.py`)
- severity: medium
- title: A custom CheckFn that forgets the `.expected` tag silently bypasses the ENTIRE stale guard — FIX reruns SHIP on attempt-1 files
- evidence: `nine/runtime/workflows.py:683-685` audits only `getattr(fn, "expected", None) or []`; the provenance tag exists only because the stock factories attach it (`nine/gates/evidence.py:101,141,176`), and NOTHING enforces it on a hand-written CheckFn — the extension path the compose/meta-workflow plugin API exists for. Repro `/tmp/t17/repros.py` R3: attempt 1 produces `solution.py` (good) but no FLAG → FIX; attempt 2 produces FLAG.txt only; `code_check` (a plain `(ctx, workdir)` CheckFn reading `solution.py`, no `.expected`) passes on the attempt-1 file; gate SHIPs — `verdict: SHIP | attempts: 2 | shipped manifest: ['FLAG.txt'] | certified solution.py in manifest? False | attempt-1 file still on disk: True`. That is the exact t7-F1/t10-F2 failure class ("certifying evidence missing from the shipped manifest") the guard was built to kill, reachable by forgetting one attribute.
- impact: the guarantee "a SHIP must have produced its evidence THIS attempt" only holds for checks that opt in; a plugin author reading files without the tag silently re-opens the stale-file hole with zero diagnostics — the debug FIX loop chases the wrong thing.
- suggested_fix: enforce the contract at the boundary: in `EvidenceGate.register_check` (or `evaluate`), emit a LOUD warning (stderr + included in `eval_results`/summary) for every check that lacks `.expected`, and/or make the stale guard refuse SHIP when ANY registered check has no `.expected` (conservative: certifies nothing is not provable for a file-reading fn). Regression test: R3 shape with the warning in place (and with the guard refusing) → today SHIP, expected FIX/BLOCK.
- effort: S

## FINDING 3
- area: bench timeout cleanup — pid identity gate (`bench/bench_nine.py`)
- severity: medium
- title: `_kill_node_groups`: a one-field pid-file line bypasses identity gate 2 and SIGKILLs an innocent session leader
- evidence: `bench/bench_nine.py:459-462` — `if start is not None:` gates the epoch check; a line with no second field gives `start=None` and the killer proceeds on the session-leader check alone, contradicting the function's own docstring ("Stale pids whose start time can no longer be verified are skipped conservatively", `:437-439`). Repro `/tmp/t17/repros.py` R6: live innocent `subprocess.Popen(["sleep","60"], start_new_session=True)`; pid file contains only `<pid>` (one field — reachable from a torn read during `_prune_node_pid`'s truncate-then-write at `nine/runtime/workflows.py:185-204`, from pre-slice-34 legacy pid files on a reused workdir, or from node-written `.nine-node-pids` files that `workdir.rglob` picks up): `kill returned: 1 | innocent process was KILLED by cleanup (false kill)`. Secondary wart in the same gate: the ±3.0s tolerance (`:460-462`) means any recycled-pid session leader started within 3s of a stale epoch is also killed.
- impact: the bench's per-fixture timeout cleanup can kill an unrelated process group on the machine — the exact failure the T15-F9 fix claims to prevent, still reachable through the format edge.
- suggested_fix: treat `start is None` as unverifiable → `continue` (skip, conservative), and shrink the tolerance to ~1.0s (recorded epoch and `/proc` starttime are the same wall clock; 3s buys nothing except false kills). Regression test: R6 shape — one-field line with a live innocent session leader → expect NOT killed (today: killed).
- effort: S

## FINDING 4
- area: bench timeout cleanup — pid-file parsing (`bench/bench_nine.py`)
- severity: medium
- title: `_kill_node_groups`: `float(parts[1])` on a garbage second field raises ValueError and aborts the whole timeout cleanup
- evidence: `bench/bench_nine.py:445` — `start = float(parts[1]) if len(parts) > 1 else None`; a line like `99999 garbage` (digit first, non-numeric second) raises `ValueError` which is NOT caught (only `OSError` at `:446-448`), aborting the loop before later pid files are processed. Repro `/tmp/t17/repros.py` R5: `_kill_node_groups RAISED: ValueError | could not convert string to float: 'garbage'`. The content is attacker/node-controlled: the killer scans `workdir.rglob(".nine-node-pids")` (`:437`) which includes any file a bash node writes with that name in a subdir (the manifest ignores the name, but the KILLER reads it), and the runtime's own file can be torn mid-write by `_prune_node_pid`'s read-modify-write.
- impact: on a fixture timeout the cleanup crashes mid-sweep — the remaining orphaned bash-node groups are never killed and keep writing into the abandoned job dir (the ghost-writer DoS the cleanup exists to stop), and the bench fixture run itself raises.
- suggested_fix: wrap the `float()` in try/except and skip the line (it is already the code's stated intent — "garbage line: skip" at `:443`). Regression test: pid file with `12345 notanumber` → cleanup returns normally and still kills entries in OTHER pid files (today: crash before reaching them).
- effort: S

## FINDING 5
- area: bench runner conversion — per-call-site constant snapshots (`bench/bench_nine.py`)
- severity: medium
- title: `_constant_snapshots` ignores AugAssign — a constant mutated between test() calls is inlined stale and INVERTS the bench verdict both ways
- evidence: `bench/bench_nine.py:120-152` snapshots only `ast.Assign` (`:143`); `EXPECTED += 1` (or `del EXPECTED`) between calls mutates the runner's value without updating the snapshot. Repro `/tmp/t17/r8c.py` with runner `EXPECTED = 1; test("a", …); EXPECTED += 1; test("b", lambda: g(), EXPECTED)` (plus the mandatory `from implementation import f, g`): generated code asserts `g() == 1` for test_b while the runner asserts `g() == 2` — `CORRECT code (g->2) -> {'test_02_b': 'FAIL(assert)'}` (false red on correct code, sending the debug FIX loop after a phantom bug in a seeded test the model cannot edit) and `BROKEN code (g->1) -> {'test_02_b': 'PASS'}` (green on broken code, reporting a passing bench for a broken starter). Both directions of the T15-F10 failure, one token (`+=`) away from the fixed shape.
- impact: bench correctness is inverted for accumulator-style expected values (counting/aggregate fixtures are the natural place for `+=`); a broken fixture can pass the converted suite and a correct one can fail it.
- suggested_fix: in `_constant_snapshots`, also track `ast.AugAssign` targets (apply the binary op to the current `ast.Constant` value) and remove targets on `ast.Delete`; add a hermetic test with the R8 runner asserting `test_02_b` inlines `2`. (Watch `str`/`int` mixes; `EXPECTED += 1` on an int is the common case.)
- effort: S

## FINDING 6
- area: schema boundary — date-time format checker (`nine/schema_validation.py`)
- severity: low
- title: `_check_date_time` accepts naive, date-only, and partial strings — the "RFC 3339 subset" claim is unmet at every boundary
- evidence: `nine/schema_validation.py:59-66` — `datetime.fromisoformat(value.replace("Z", "+00:00"))` accepts `"2026-08-13T12:00:00"` (no offset — RFC 3339 REQUIRES a time-offset), `"2026-08-13"` (date only), `"2026-08"`, and even `"2026"` (all resolve to naive midnight). Repro `/tmp/t17/repros.py` R7: all eight probes VALID, and end-to-end `is_valid("agent-job", {…, "created_at": "2026-08-13", "updated_at": "2026-08-13T12:00:00", …})` → True. The fix comment (`:50-56`) promises "garbage timestamps fail validate() at every boundary" — `verified_at`/`produced_at`/`created_at`/`updated_at`/route-decision timestamps from any foreign writer (hand-edited ledger line, Firestore adapter, plugin gate emitting `datetime.now()` naive) validate cleanly.
- impact: durable records can carry tz-less/date-only timestamps; consumers comparing with aware datetimes (`datetime.now(UTC)`) raise `TypeError` and RFC 3339 parsers reject — analytics sort by `created_at` (`nine/ledger/ledger.py:246`) silently misorders. Low because in-repo writers always emit `datetime.now(UTC).isoformat()` (offset present).
- suggested_fix: in `_check_date_time`, require a time component and a tz marker: parse via `fromisoformat` after `Z`-normalization, then require `dt.tzinfo is not None` and (for the date-only family) that a `"T"`/`"t"` separator exists before the time part. Regression test: assert `validate` rejects `"2026-08-13"`, `"2026-08"`, `"2026"`, and naive `"…T12:00:00"`, accepts `"…T12:00:00Z"` and `"…T12:00:00+00:00"` (and ideally lowercase `z`, which `fromisoformat` already accepts).
- effort: S

## FINDING 7
- area: stale-artifact guard — symlink handling (`nine/runtime/workflows.py`)
- severity: low
- title: False BLOCK: a symlink at an expected input whose TARGET was produced this attempt inside the job dir can never ship
- evidence: `nine/runtime/workflows.py:688-697` treats ANY symlink at an expected path as stale, even when the symlink's target is a legitimately produced, registered inside file — the guard's own rationale ("the file is absent from the shipped manifest", `:693-695`) is factually wrong for an inside target, whose CONTENT is in the manifest under the target's name. Repro `/tmp/t17/repros.py` R4: tool produces `REPORT.md` (registered) plus `latest.md -> REPORT.md` (the natural "latest → versioned file" pattern); a custom check certifies `latest.md` (content = REPORT.md's registered bytes): `verdict: BLOCK | summary: stale artifact(s): ['latest.md'] | manifest names: ['REPORT.md']`. The T15-F1 fix closed the outside-target hole but also blocks the safe inside-target case with no escape hatch (BLOCK is terminal — no fix loop), making any symlink-producing workflow permanently unshippable.
- impact: false negative on a safe workflow — operator must manually recover/delete a legit artifact pattern; wasted time and confusion ("why does my produced file keep BLOCKing?").
- suggested_fix: when `p_expected.is_symlink()`, resolve the target; if the resolved path is inside `job_dir` AND its relative name is in `registered` (produced this attempt), treat as produced (`continue`); otherwise stale. Regression test: R4 shape → SHIP; outside-target control → still BLOCK.
- effort: S

## FINDING 8
- area: outside-artifact namespacing (`nine/runtime/workflows.py`)
- severity: low
- title: `../<basename>` namespace still collides outside-vs-outside — two different outside files with the same basename silently replace each other
- evidence: `nine/runtime/workflows.py:641` — outside rel = `"../" + p.name` (basename only). Two nodes certifying DIFFERENT outside files `x/report.md` and `y/report.md` both map to `../report.md`; the same-name replace at `:620-621` (`seen_idx`) silently drops the first. Repro `/tmp/t17/repros.py` R9: `artifact: ../report.md -> /tmp/t17/r9/y/report.md` — ONE manifest entry, `x/report.md` vanished with no warning. The T15-F5 fix namespaced outside-vs-inside but not outside-vs-outside; a multi-tool workflow certifying two same-named external references (two review sources, two data exports) silently loses one from the manifest and from `evidence_refs` replay.
- impact: silent loss of a certified artifact from the durable record — the same "manifest omits evidence" class the fix was built to kill, one level out.
- suggested_fix: include the parent segment(s) in the outside namespace (`"../" + p.parent.name + "/" + p.name`), or disambiguate with a short path digest when the basename repeats. Regression test: R9 shape → TWO entries (`../x/report.md`, `../y/report.md`).
- effort: S

---

### Summary
8 findings (2 medium-high value in the core stale-guard family — content TOCTOU + opt-in provenance tag; 3 in the bench cleanup/conversion machinery; 1 schema boundary; 2 low-severity guard edges). Every one is a hole in a slice-34 fix, reproduced hermetically against HEAD 0c92623, all fixable in <30 lines with a hermetic regression test. No style nits.
