# TORTURE-TESTER-15 Report — round 8: content-digest exemption edges, `_register`/manifest ignore, memory redaction, NON_ROUTABLE_IDS, bench pid-file cleanup, convert_to_pytest inlining, server ledger/events

Worker: TORTURE-TESTER-15 (round 8, respawn). Repo HEAD: ce658e5 (slice 33 — "15 findings ALL FIXED").
All repros hermetic (no Gemini, no network, no quota): `.venv/bin/python` scripts under
`/tmp/t15/` (r1…r8), stub/monkeypatch only, every repro uses a temp `NINE_DATA_DIR`. No repo
files were modified; the only repo write is this report (verified via `git status`).

Re-attacked surfaces = the 15 slice-33 fixes (t13 F1-F5 + t14 F1-F10). Each finding is filed
against the ORIGINAL finding text: a fix that does not survive its edge is filed as a HOLE; a
fix that survives is listed under "holds" (with warts noted).

Surfaces that HOLD after re-attack (verified by repro, not re-filed):
- Content-digest exemption core (t13-F3 / t14-F3): real modification of a seeded input that is
  NOT re-registered → BLOCK "stale artifact(s)" (repro r1c B1 CONTROL: verdict BLOCK, attempts 2,
  summary lists `note.txt`); unchanged inputs and empty files exempt without crash; a
  modified-then-restored file matches the attempt-1 digest → SHIP (correct); deleted inputs are
  skipped by the `exists()` guard. The snapshot is keyed per job_dir and each chain hop builds a
  fresh `WorkflowExecutor` (verified `nine/chains/chain.py` `_execute` — executor created once
  per hop, outside the attempt loop), so each hop re-snapshots.
- `_register` same-name replace (t14-F4): same-name same-attempt different-hash writes REPLACE —
  repro r8 shows exactly ONE manifest entry for two `EVAL.json` writers, `produced_by=n2`,
  hash = last writer = the disk state the gate certifies. Dedupe key is the RELATIVE name (t13-F4).
- Gate symlink stance: every stock gate refuses symlinks — `eval_json_check` via `load_eval_json`
  (`nine/gates/evidence.py:83` `not p.is_symlink()`), `required_artifact_check` (`:177`),
  `file_nonempty_check` (`:98`) — so the t8-F1 "symlinks are never evidence" contract is
  enforced at gate level for every in-repo workflow (respond, build, flagship hops).
- NON_ROUTABLE_IDS routing (t14-F1): the router can NEVER emit the demo lane — the
  NON_ROUTABLE drop (`nine/registry.py:350-357`) removes the exact id, the t12-F6 dead-id filter
  removes case/whitespace variants, and dispatch does not lowercase, so no production submit can
  reach `inbox-triage-task-report` (repro r4: variants dropped at merge with a loud warning;
  `classify("customer wants a refund")` → `respond`). Only a wart: the `learn apply` refusal is
  exact-match (Finding 8).
- `learn apply` string-bucket refusal (t14-F8) and memory-store shape-skip (t14-F9): code paths
  re-read; unchanged and correct.
- convert_to_pytest loud refusal of dangling helpers (t14-F5): AnnAssign `EXPECTED: int = 5` and
  aliased `import add as a2` both refuse with ONE clean `RuntimeError` naming the dangling names
  (repro r6b B/C) — the NameError-at-run-time suite class is closed; the inlining itself has a
  semantics bug (Finding 10) and the converter still crashes raw on a non-literal name arg
  (Finding 11, pre-existing).

---

## FINDING 1
- area: content-digest run-input exemption edges (`nine/runtime/workflows.py`) / gates
- severity: med
- title: The SHIP-check exemption SKIPS symlinks at expected input paths (`continue`) instead of treating the swap as a content change — with any symlink-following check (custom `CheckFn` / compose plugin / future gate) the t13-F3 modified-input guard is bypassed and a job SHIPs with the gate-certified file ABSENT from the shipped manifest
- evidence: `nine/runtime/workflows.py:614` — `if p_expected.is_symlink() or not p_expected.exists(): continue  # not evidence / the check already failed`. The executor's own docstring says symlinks are never evidence (`:424-426`, `:539-542`, `:562-565`), so a symlink at an expected path is skipped by the manifest loop AND by the stale guard: the digest comparison (`:655`) never runs against it. Repro `/tmp/t15/r1c_symlink_bypass.py` (custom CheckFn that certifies via `open()` — the same shape any file-reading plugin check uses): attempt 1 replaces the seeded `note.txt` with a symlink to an outside file; attempt 2 ships `FLAG.txt`; gate passes (follows the link), stale guard sees `is_symlink()` → continue → **SHIP, manifest = [FLAG.txt] only**. CONTROL (real modification, r1c B1): **BLOCK** `stale artifact(s): ['note.txt']`. The asymmetry is the hole: a real modification is caught, a symlink substitution is not. (In-repo stock gates all refuse symlinks — verified — so today the direct path needs a plugin/custom gate; the executor is the documented last line of defense and should not bless it.)
- impact: a run can certify outside content as its own evidence while the shipped manifest omits the certified file — the exact t7-F1/t10-F2/t13-F3 failure class ("certifying evidence missing from the shipped manifest"), reachable through the extension API the compose meta-workflow is designed to produce.
- suggested_fix: in the stale guard, treat `p_expected.is_symlink()` as MODIFIED (append to `stale`) instead of skipping — a symlink can never match the attempt-1 content digest, and the guard's own comment "not evidence" is a gate-level concern, not an executor-level exemption. Regression test: seed input → attempt-1 snapshot → attempt-2 replaces the input with a symlink → expect BLOCK with `note.txt` in `stale` (today: SHIP).
- effort: S

## FINDING 2
- area: content-digest run-input exemption edges (`nine/runtime/workflows.py`)
- severity: low
- title: Unguarded `read_bytes()` in the exemption path — an unreadable gate-certified input (chmod 000, permission drift on a volume) raises raw `PermissionError` out of `execute()`: no verdict, job stuck `running`, raw traceback/500 instead of a BLOCK
- evidence: `nine/runtime/workflows.py:655` — `if self._hash(p_expected.read_bytes()) == snap.get(expected_name)`; same unguarded read in the snapshot (`:439` `rel: self._hash(p.read_bytes())`) and the dir-artifact branch (`:636`). Repro r1 CASE C (chmod 000 on a seeded input): `PermissionError: [Errno 13] Permission denied` propagates out of `WorkflowExecutor.execute()` — the verdict is never attached, the CLI/server surfaces a raw traceback, and the job stays `running` with no terminal state.
- impact: a single unreadable seeded file DoSes the job (and, via the server submit path, a 500) instead of a clean BLOCK the fix loop can act on. The t14-F10 "one clean error" doctrine applies to every other store read; this read path is still bare.
- suggested_fix: wrap the three `read_bytes()` sites in `try/except OSError` and treat an unreadable input as `stale` (BLOCK) — an unreadable file is not unchanged content, so the exemption must not apply. Regression test: seed input with `chmod 000` → SHIP attempt → BLOCK (or at minimum a clean terminal verdict), never a raise.
- effort: S

## FINDING 3
- area: `_register` replace semantics + manifest ignore holes (`nine/runtime/workflows.py`)
- severity: med
- title: The explicit `artifact_path`/`artifact` branch bypasses the t14-F4 manifest ignore lists — a tool node can re-certify `.log`, `.pyc`, `.pytest_cache/*`, or `.nine-node-pids` as shipped evidence, undoing F4 at the one registration point that is fully under tool control
- evidence: `nine/runtime/workflows.py:557-579` — the explicit branch calls `_register(rel, str(p), "other", ...)` with NO consultation of `_MANIFEST_IGNORE_DIRS/SUFFIXES/NAMES` (`:117-119`); the ignore predicates live only inside `_manifest_files` (`:139-142`). Repro `/tmp/t15/r2_register_manifest.py` R2-A/B: a tool node returning `artifact_path` for `test_output.log` registers it (`kind: other`, in manifest, SHIP) and even `.nine-node-pids` registers (`kind: other`). The F4 commit message promises these byproducts are "never evidence"; the explicit path is the one a tool can name freely.
- impact: shipped manifests and `nine artifacts`/memory lineage can again contain pytest cache, `.pyc`, log files, and the runtime's own pid tracker — the exact pollution t14-F4 removed from the recursive inventory, re-added through the documented tool output surface.
- suggested_fix: apply the same three predicates in the explicit branch (skip when any path part is an ignore dir, suffix matches case-insensitively, or the relative name is `.nine-node-pids`); better, factor a shared `_is_ignored(rel, p)` used by both `_manifest_files` and the explicit branch. Regression test: tool returns `artifact_path` pointing at `test_output.log` / `.nine-node-pids` → no manifest entry, verdict still SHIP on other evidence.
- effort: S

## FINDING 4
- area: `_register` manifest ignore holes (`nine/runtime/workflows.py`)
- severity: low
- title: Ignore-list gaps: a NESTED `.nine-node-pids` (`sub/.nine-node-pids`) and case-variant suffixes (`output.LOG`) are manifest evidence — the name ignore matches the full relative path only, and suffix matching is case-sensitive
- evidence: `nine/runtime/workflows.py:141` — `if rel in _MANIFEST_IGNORE_NAMES or p.suffix in _MANIFEST_IGNORE_SUFFIXES`. Repro r2-C: `sub/.nine-node-pids` → `listed=True`; `output.LOG` → `listed=True`; while `.nine-node-pids`, `test_output.log`, `__pycache__/x.pyc`, `sub/__pycache__/x.pyc`, `.pytest_cache/entry.json`, `.git/config` → all `listed=False`. A bash node that runs `subdir` work (or writes `output.LOG` itself) re-pollutes the manifest through the recursive inventory F4 was meant to clean.
- impact: low — same pollution class as Finding 3, via the recursive path; the `.nine-node-pids` variant is notable because a node can create the file in a subdir (the runtime itself only writes it at the job-dir top level, `workflows.py:156`).
- suggested_fix: match the name ignore on ANY path part (`any(part == ".nine-node-pids" for part in parts)`) and casefold suffixes (`p.suffix.lower() in _MANIFEST_IGNORE_SUFFIXES`); share the predicate with Finding 3's fix.
- effort: S

## FINDING 5
- area: `_register` replace semantics (`nine/runtime/workflows.py`)
- severity: low
- title: An `artifact_path` OUTSIDE the job dir registers under its BASENAME and can silently REPLACE a same-named inside-file entry — the shipped manifest's path then points outside the job dir and the inside file's certification is dropped
- evidence: `nine/runtime/workflows.py:575-579` — `try: rel = p.relative_to(job_dir).as_posix() except ValueError: rel = p.name`; the replace logic (`:527-532`) then treats `rel` as a collision key. Repro `/tmp/t15/r8_register_edges.py` (case 2): the job dir contains `hosts` (INSIDE content, hash `6f0d721d53`), the tool certifies `/tmp/.../outside/hosts` (OUTSIDE content, hash `534e60b74f`); the manifest ends with ONE `hosts` entry — `path: /tmp/.../outside/hosts`, hash `534e60b74f` (the OUTSIDE file). The inside file's registration was replaced by a file whose path leaves the job dir.
- impact: the manifest (and `nine artifacts`, memory lineage, evidence replay) can point outside the job dir with no marker; combined with the `_artifact_summary` fallback (Finding 7) the same artifact's memory record reads the WRONG file's content head. This is the t8-F1 "outside content certified as this job's evidence" class reached through a name collision instead of a symlink.
- suggested_fix: namespace external rels (e.g. `rel = "../" + p.name` or a stable prefix) so an outside artifact can never collide with an inside rel; or refuse outside `artifact_path` when a same-named inside file is already registered. Regression test: inside `hosts` + outside `artifact_path=hosts` → two distinct manifest entries (or a refusal), never a silent replace.
- effort: S

## FINDING 6
- area: memory redaction (`nine/chains/chain.py` `_save_memory` + `nine/router/classifier.py` `redact()`)
- severity: med
- title: `redact()` still lets common credential shapes through — URL-embedded creds (`mongodb://admin:hunter2@…`), the space-separated form (`--password hunter2`), and `Authorization: Basic <base64>` land VERBATIM in memory.jsonl artifact summaries, so the t14-F2 "no credentials in memory.jsonl" promise holds only for the tested shapes (aws/API_KEY/Bearer/GITHUB_TOKEN)
- evidence: `nine/chains/chain.py:336-345` (`_artifact_summary` → `redact(head)` of the artifact's 400-char head) + `nine/router/classifier.py:48-79` (pattern list; `redact()` docstring: "not a security boundary"). Repro `/tmp/t15/r3_memory_redaction.py`: OUT.md summary written to memory.jsonl =
  `PREFIX\naws_secret_access_key=***\nAPI_KEY=***\n--password hunter2\nmongodb://admin:hunter2@db.example.com:27017\nAuthorization: Basic dXNlcjpwYXNz\nGITHUB_TOKEN=***\nBearer ***\nSUFFIX\n`
  — the first three lines are the t14-F2 shapes (correctly redacted), the middle three LEAK: `--password` is not matched (patterns require `[=!~]=`, `[=:]`, or `is|was|:=|:|=` separators — not a bare space), URL userinfo is not matched by any pattern, and only `Bearer` (not `Basic`) is covered.
- impact: on Cloud Run memory.jsonl is Firestore; a model echoing a task's `mongodb://user:pass@host` URI or a Basic-auth header into an artifact stores live credentials in the durable memory store — the exact leak class t14-F2 was filed for.
- suggested_fix: add patterns: `(password|passwd|pwd|secret|token)\s+\S+` for the space form (with a value-length guard), URL userinfo `([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@` → `\1***:***@`, and `Authorization:\s*Basic\s+[A-Za-z0-9+/=]+` → `Authorization: Basic ***`. Regression test: battery of 15 shapes including URL creds / Basic / space-form / JSON-quoted / comparison tails — all redacted.
- effort: S

## FINDING 7
- area: memory redaction / artifact lineage (`nine/chains/chain.py` `_artifact_summary`)
- severity: low
- title: Outside-job-dir artifacts get the HANDOFF.md fallback as their memory summary — the t14-F2 "use the ARTIFACT's own content head" fix still misattributes every `artifact_path` artifact that lives outside the job dir
- evidence: `nine/chains/chain.py:331-349` — `_artifact_summary` reads `src = job_dir / name` (the artifact's RELATIVE name, not its registered `path`); for an outside artifact `job_dir / name` does not exist → falls to `handoff = job_dir / "HANDOFF.md"` → the plan hop's handoff becomes the summary. Repro `/tmp/t15/r3_memory_redaction.py`: the `hosts` artifact (certified via `artifact_path` pointing outside the job dir, per Finding 5) records `summary: 'PLAN HANDOFF CONTENT v1\n'` in memory.jsonl — the plan hop's content, exactly the cross-hop misattribution t14-F2 removed for inside artifacts, still open for outside ones.
- impact: memory lineage for outside artifacts attributes the plan handoff as their content (same class as t14-F2's headline complaint); also means a summary can be stored for an artifact whose own content was never read.
- suggested_fix: resolve the summary source from the artifact's registered `path` (`art["path"]`), falling back to `job_dir / name` only for inside artifacts, and to HANDOFF.md only when the artifact file is genuinely gone. Regression test: outside `artifact_path` artifact → memory summary = its own content head, not the handoff.
- effort: S

## FINDING 8
- area: NON_ROUTABLE_IDS (`nine/registry.py` / `nine/cli.py` `_apply_candidate`)
- severity: low
- title: `NON_ROUTABLE_IDS` enforcement is exact-match — a case/whitespace variant of the demo id passes `nine learn apply` (marked applied, junk catalog bucket) and is only caught later at merge time; routing itself stays airtight (verified)
- evidence: `nine/cli.py:614` `if wf_id in NON_ROUTABLE_IDS:` (exact match, no `strip()`/`casefold()`) vs `nine/registry.py:350-357` (same exact match at merge). Repro `/tmp/t15/r4_non_routable.py`: catalog with `inbox-triage-task-report`, `Inbox-Triage-Task-Report`, `inbox-triage-task-report ` (trailing space) and a legit lane → merge output shows all three demo variants DROPPED with warnings ("keyword entries for non-routable workflow id 'inbox-triage-task-report' dropped" / dead-id "removed plugin?"), `research-plan-build-review-teach` kept, and `classify("customer wants a refund")` → `('respond', 0.0, ...)` — the demo lane is NOT reachable (fix HOLDS). But `_apply_candidate` would have accepted the variant as applied before the merge ever sees it.
- impact: low — routing is safe (dead-id filter + no lowercase in dispatch); the cost is a silently-useless applied candidate and a catalog bucket that can never route, plus a confusing "applied" confirmation for an entry that does nothing.
- suggested_fix: normalize (`wf_id.strip().casefold()`) before the NON_ROUTABLE check in BOTH `_apply_candidate` and `_merged_keywords`, and warn when a normalized drop happens. Regression test: apply candidate with `"Inbox-Triage-Task-Report "` → refused with the non-routable error (today: accepted).
- effort: S

## FINDING 9
- area: bench `_kill_node_groups` pid-file robustness (`bench/bench_nine.py` + `nine/runtime/workflows.py` `_record_node_pid`)
- severity: med
- title: Pid-file cleanup kills innocent session leaders — pids are recorded for EVERY bash node, NEVER removed on normal completion, and `_kill_node_groups` kills without any identity validation: a recycled pid (node exited, OS reused the number within the run window) SIGTERM+SIGKILLs an unrelated process group on a busy box
- evidence: `nine/runtime/workflows.py:147-160` (`_record_node_pid` appends `proc.pid`, no removal anywhere — grep confirms the only writer is `:268`, the pid file persists after the node completes), `:257-260` (Popen `shell=True`, `start_new_session=True` → recorded pid IS the session leader). `bench/bench_nine.py:356-384` `_kill_node_groups` — `os.killpg(pid, SIGTERM)` → 0.2s → `SIGKILL`, catching only ProcessLookupError/PermissionError/OSError; no `os.getsid(pid) == pid` check, no start-time/executable check. Repro `/tmp/t15/r5_kill_node_groups.py`: plant a LIVE innocent `bash -c 'sleep 30'` session leader's pid in `.nine-node-pids` → `_kill_node_groups` reports `killed: 1` and the innocent process is DEAD; control (non-session-leader pid) survives (killpg is a no-op). The runtime's own timeout path kills the group correctly (`workflows.py:279-284`); the external-killer path is the unvalidated one.
- impact: within a single fixture run, every bash node leaves a stale pid in the file for the rest of the run; when a per-fixture timeout fires after pid reuse (busy CI/multi-daemon host — this project runs concurrent agent daemons), the cleanup kills whatever innocent process now owns that pid as a session leader (another agent's daemon, a pytest worker, a build step).
- suggested_fix: record `(pid, start_time)` at spawn (or verify `os.getsid(pid) == pid` + `/proc/<pid>/stat` start tick matches) before killing; and prune the pid file when a node completes normally (the runtime knows the node is done — remove its line). Regression test: pid file containing (a) a live non-leader pid → untouched, (b) a recycled leader pid whose start time differs → untouched, (c) the actual orphaned group → killed.
- effort: M

## FINDING 10
- area: `convert_to_pytest` inlining (`bench/bench_nine.py`)
- severity: med
- title: Constant inlining captures the LAST module-level assignment, not the value at the call site — a runner that reassigns a constant between test() calls converts to a pytest suite asserting the WRONG contract (runs green on broken code, red on correct code)
- evidence: `bench/bench_nine.py:119-134` `_runner_constants` — a dict of name → last `ast.Constant` in module order; `:214-219` — every `Load` of that name is replaced by the dict value. Repro `/tmp/t15/r6_convert_pytest.py` case A: `EXPECTED = 5; test("a", lambda: add(2,3), EXPECTED); EXPECTED = 6; test("b", ...)` converts BOTH tests to `assert add(2, 3) == 6`. The eager runner captured 5 for test a at call time — so with a CORRECT `add` (returns 5) the converted suite FAILS test_01_a while the authoritative check.sh PASSES it; with a BROKEN `add` returning 6 the converted suite passes and check.sh fails. Case F (`X = 1; test(..., X); X = 2`) shows the same divergence in the expected-arg position (`== 2` inlined where the runner used 1).
- impact: the debug lane's `test_solution.py` asserts the wrong contract for any runner that reassigns a constant — the exact "fix-loop chasing a bug in the seeded test file" failure t14-F5 was filed to prevent, now via wrong VALUES instead of NameErrors. (Multi-target `A = B = 5` works — verified.)
- suggested_fix: walk the module body in order and snapshot each constant's value at each test() call site (only inline the value in effect at that point), or refuse conversion loudly when a constant is reassigned after first use. Regression test: the A/F runners convert to `== 5` / `== 1` for the first call.
- effort: M

## FINDING 11
- area: `convert_to_pytest` inlining (`bench/bench_nine.py`)
- severity: low
- title: A non-literal first argument to test()/test_raises() (e.g. `test_raises(EXC, ...)` with `EXC = ValueError`) crashes `convert_to_pytest` with a raw `ValueError: malformed node or string` traceback — pre-existing, but it violates the t14-F5 "fail loudly with ONE clean error" contract for the converter
- evidence: `bench/bench_nine.py:223` — `name = ast.literal_eval(args[0]) if args else f"case_{idx}"`; `ast.literal_eval` raises `ValueError` on any non-literal AST node. Repro `/tmp/t15/r6b_convert_pytest2.py`: `EXC = ValueError; test_raises(EXC, lambda: f())` → unhandled `ValueError: malformed node or string on line 3: <ast.Name ...>` traceback (the exception-constant is a documented runner pattern; `test_raises(ValueError, ...)` control converts fine). `bench_nine.main` catches it (`[warn] test conversion failed`), so the bench degrades gracefully — but the converter's own contract is a clean refusal.
- impact: low — a raw traceback in the conversion step for a legit runner pattern; the F5 fix's "never emit a suite that NameErrors" promise is met by crash, but not by the promised clean RuntimeError.
- suggested_fix: wrap the `literal_eval` in try/except (ValueError, TypeError) and fall back to a slug from `ast.unparse(args[0])` or a clean refusal naming the offending call. Regression test: runner with `test_raises(EXC, ...)` → clean conversion (slugged name) or clean RuntimeError, never a traceback.
- effort: S

## FINDING 12
- area: server `LedgerUnavailable` coverage (`deploy/server.py` + `nine/learn/learner.py`)
- severity: med
- title: t14-F10 is incomplete — `GET /v1/events` raw-500s on a bad NINE_DATA_DIR because `get_learner()` (RouteEventStore construction, `path.parent.mkdir`) is not wrapped: `/v1/jobs`, `/v1/jobs/{id}`, `/v1/stats`, POST `/v1/submit` all return the promised clean 502 JSON, the events endpoint returns `Internal Server Error`
- evidence: `deploy/server.py:467-473` — `events()` calls `get_learner()`; `:156-161` `get_learner` returns `Learner(RouteEventStore(EVENTS_PATH))` with no OSError handling; `nine/learn/learner.py:71` — `self.path.parent.mkdir(parents=True, exist_ok=True)` raises `NotADirectoryError` when `NINE_DATA_DIR` is a file. The F10 latch/handler covers `get_ledger` only (`:169-209`, `:128-131`). Repro `/tmp/t15/r7b_server_ledger.py` (NINE_DATA_DIR = a regular file, TestClient with `raise_server_exceptions=False`): `GET /health → 200`, `GET /v1/jobs → 502 {"detail":"cannot read ledger ..."}`, `GET /v1/jobs/abc-123 → 502`, `GET /v1/stats → 502`, `POST /v1/submit → 502`, **`GET /v1/events → 500 'Internal Server Error'`**.
- impact: the F10 commit message promises "bad NINE_DATA_DIR -> clean JSON 502 with the reason on every ledger endpoint"; `/v1/events` is a ledger-family endpoint and still 500s opaquely (and every request to it re-raises the mkdir error). Monitoring/LEARN consumers hitting `/v1/events` see a raw 500, the exact failure mode F10 set out to kill.
- suggested_fix: wrap `get_learner()` (and `get_memory()` — `MEMORY_PATH` has the same shape) store construction in the same `except Exception → LedgerUnavailable` path as `get_ledger`, or guard `_RUNTIME` once at startup. Regression test: bad NINE_DATA_DIR → `/v1/events` returns 502 JSON with the reason like the other endpoints.
- effort: S

## FINDING 13
- area: server `LedgerUnavailable` / fallback latch (`deploy/server.py` `_LazyFallbackLedger`)
- severity: low
- title: The per-request Firestore retry persists when the JSONL fallback WORKS — `_ledger_failed` latches only on fallback CONSTRUCTION failure, so a healthy JSONL fallback with a broken Firestore still attempts Firestore on EVERY request (the t14-F10 "no per-request Firestore retry storm" is only half-achieved)
- evidence: `deploy/server.py:211-257` — `_LazyFallbackLedger.__getattr__` wrapper calls the primary first on every method call; the latch (`_ledger_failed = True`) and the warning print happen ONLY inside `if self._fallback is None:` (`:234-243`) and only the CONSTRUCTION-failure branch (`:252-253`) latches. Repro `/tmp/t15/r7c_server_ledger2.py`: primary that raises on every `discover()`, fallback pre-constructed (healthy disk) → 3 requests → **3 Firestore attempts** (`Firestore attempts=3`), zero warnings after the first (the warning is gated by `_fallback is None`). `get_ledger()` keeps returning the wrapper because `_ledger_failed` is False.
- impact: with Firestore configured-but-down and a healthy disk fallback (the emulator-misconfig case F10 was built for), every request pays a Firestore round trip (up to the gRPC timeout) before serving from JSONL — latency and quota cost that persists for the life of the process, plus `/health` stays 200 while the ledger is degraded (residual from t14-F10, still open).
- suggested_fix: latch once the fallback is engaged (`_ledger_failed = True` after the first successful fallback swap) so `get_ledger()` returns the plain JSONL ledger on subsequent requests; optionally surface `degraded: true` in `/health` when latched. Regression test: primary always-fails + healthy fallback → request 1 attempts primary once, requests 2+ do not touch the primary.
- effort: S

---

## Verdicts vs slice-33 promises (quick table)

| slice-33 fix (original text) | verdict | residual |
|---|---|---|
| t13-F2 ghost-writer cleanup (pid-file groups) | PARTIAL | Finding 9 — no identity validation, stale pids accumulate |
| t13-F3 content-based run-input exemption | HOLDS core / HOLE edge | Finding 1 (symlink swap) + Finding 2 (unreadable input crash) |
| t13-F4 explicit artifact_path dedupe (relative name) | HOLDS | Finding 5 (outside basename collision) |
| t14-F1 NON_ROUTABLE_IDS | HOLDS | Finding 8 (exact-match apply wart) |
| t14-F2 redacted per-artifact memory summaries | PARTIAL | Findings 6-7 (missed cred shapes, outside-artifact misattribution) |
| t14-F4 manifest ignore lists | PARTIAL | Findings 3-4 (explicit-path bypass, nested/case holes) |
| t14-F5 convert_to_pytest inline+refuse | PARTIAL | Findings 10-11 (last-value inlining, raw ValueError crash) |
| t14-F10 bad-NINE_DATA_DIR clean 502 + latch | PARTIAL | Findings 12-13 (/v1/events 500; per-request Firestore retry persists) |

13 findings: 6 med (1, 3, 6, 9, 10, 12), 7 low. No high this round — the two high-severity
slice-33 fixes (demo-lane routing ban, memory raw-handoff leak) hold at their core; the
residuals are edge/extension-API-shaped.

## Hygiene

- All repros ran against temp `NINE_DATA_DIR` / temp dirs only; the repo `jobs/` was never
  touched. `git status` after the round shows the new report plus the PRE-EXISTING
  `bench/state.json` modification (mtime 01:16, before this round's repros at 01:24+ —
  the same pre-existing modification noted in the torture-14 hygiene note).
- Repro scripts kept in `/tmp/t15/` (r1, r1b, r1c, r2…r8) for triage; each prints its own
  evidence block.
- Effort estimate note: Findings 1, 3, 4, 6, 8, 11-13 are S (one-line predicate/pattern or a
  shared-guard refactor); Findings 9-10 are M (identity bookkeeping at spawn/complete or
  call-site-value tracking).
