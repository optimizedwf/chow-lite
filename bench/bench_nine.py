#!/usr/bin/env python3
"""bench_nine.py — benchmark nine (agent OS) against chow-agent-evals bugfix fixtures.

For each fixture bugfix-small-001..009:
  1. create an isolated bench dir under bench-runs/<fixture>/
  2. seed the nine job dir with the fixture starter (solution.py) plus a pytest
     test_solution.py converted 1:1 from the fixture's own tests/check.sh
     assertions (so the debug lane's verify node + fix loops run REAL tests)
  3. run the REAL CLI entry point: .venv/bin/nine submit --workdir ... "<task>"
     (a watcher thread seeds the job dir the instant it appears; the patch
      node and any fix-loop re-runs re-read the files, so late seeding is safe)
  4. wait for the verdict (SHIP/FIX/BLOCK) and final job status
  5. independently run the fixture's own tests/check.sh against the produced
     patch.py and count tests passed / total
  6. record {fixture, routed_workflow, verdict, tests_passed, tests_total,
     duration_s, attempts, final_status} and write bench-runs/results.json

Secrets policy: GEMINI_API_KEY is read from ~/.agent-vault/keys/gemini.key
(the path is referenced only; the value is never printed or persisted).
"""
from __future__ import annotations

import ast
import builtins
import json
import os
import re
import shutil
import signal
import datetime
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO / "bench" / "fixtures"
KEY_PATH = Path(os.environ.get("NINE_BENCH_KEY", os.path.expanduser("~/.agent-vault/keys/gemini.key")))
NINE_BIN = REPO / ".venv" / "bin" / "nine"
GEMINI_MODEL = "gemini-3.6-flash"
_env_fx = os.environ.get("NINE_BENCH_FIXTURES", "")
FIXTURES = _env_fx.split(",") if _env_fx else [f"bugfix-small-{i:03d}" for i in range(1, 10)]

TASK_MODE = os.environ.get("NINE_BENCH_TASK_MODE", "full")  # full | desc
RUNID = os.environ.get("NINE_BENCH_RUNID", "r0")


def task_from_md(md_text: str, mode: str = "full") -> str:
    """full: the entire task.md; desc: only the '## Task Description' section."""
    if mode != "desc":
        return md_text.strip()
    lines = md_text.splitlines()
    out, in_sec = [], False
    for ln in lines:
        if ln.startswith("## Task Description"):
            in_sec = True
            continue
        if in_sec and ln.startswith("## "):
            break
        if in_sec:
            out.append(ln)
    return "\n".join(out).strip() or md_text.strip()
BENCH_ROOT = REPO / "bench" / "runs"
PER_FIXTURE_TIMEOUT_S = 1500.0

# ---------------------------------------------------------------- key load ---
def load_api_key() -> str:
    """Key for the ACTIVE backend (torture-10 F6).

    gemini backend (default): GEMINI_API_KEY from NINE_BENCH_KEY ->
    ~/.agent-vault/keys/gemini.key (path-only; the value is never printed).
    openai backend (NINE_LLM_BACKEND=openai, slice-28 testing mode): mirror
    llm_provider.api_key() — NINE_LLM_API_KEY -> OPENCODE_GO_API_KEY ->
    ~/.agent-vault/keys/opencode-go.key -> ~/.prime/agent/auth.json
    [opencode-go]. The gemini key file is NOT required on the openai backend.
    """
    if os.environ.get("NINE_LLM_BACKEND", "").strip().lower() in ("openai", "opencode", "rue"):
        from nine.runtime import llm_provider

        key = llm_provider.api_key()
        if not key:
            sys.exit("FATAL: NINE_LLM_BACKEND=openai but no tunnel key found "
                     "(NINE_LLM_API_KEY -> OPENCODE_GO_API_KEY -> "
                     "~/.agent-vault/keys/opencode-go.key -> auth.json "
                     "[opencode-go]); reference the path only, never inline keys")
        return key
    if not KEY_PATH.exists():
        sys.exit(f"FATAL: key file not found at {KEY_PATH} (reference the path only, never inline keys)")
    with open(KEY_PATH, encoding="utf-8") as fh:
        key = fh.read().strip()
    if not key:
        sys.exit(f"FATAL: key file at {KEY_PATH} is empty")
    return key

# ----------------------------------------------------- check.sh -> pytest ----
def extract_runner(check_sh: Path) -> str:
    """Pull the embedded python test-runner heredoc (the one with test(...) calls)."""
    text = check_sh.read_text(encoding="utf-8")
    blocks = re.findall(r"<<'PYEOF'\n(.*?)\nPYEOF", text, re.DOTALL)
    runner = [b for b in blocks if "tests.append" in b]
    if not runner:
        raise RuntimeError(f"no test-runner heredoc found in {check_sh}")
    return runner[0]

def _imported_names(runner_src: str) -> list[str]:
    tree = ast.parse(runner_src)
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("implementation"):
            names.extend(a.name for a in node.names if a.name != "*")
    if not names:
        raise RuntimeError("no `from implementation import ...` found in runner")
    return names

def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return (s or "case")[:48]

def _constant_snapshots(tree: ast.Module,
                        calls: list[ast.Call]) -> list[dict[str, ast.Constant]]:
    """For each top-level test()/test_raises() call, the module-level
    literal constants IN EFFECT AT THAT CALL SITE.

    torture-14 F5: the converted pytest suite only imports names from
    `solution`; a runner-local constant referenced by a test expression
    (EXPECTED_SUM = 5) would NameError at RUN time. Names that resolve to a
    module-level literal are inlined into the converted tests.
    torture-15 F10: the runner may REASSIGN a constant between calls
    (EXPECTED = 5; test(...); EXPECTED = 6; test(...)) — inlining the LAST
    assignment for every call asserts the wrong contract (green on broken
    code, red on correct code). Snapshot the value in effect at each call.

    NOTE: takes the ALREADY-PARSED tree (not source text) — matching by
    object identity across two separate ast.parse() calls NEVER matches
    (fresh AST objects get fresh ids), which silently returned zero
    snapshots and left every runner constant dangling.
    """
    call_ids = {id(c) for c in calls}
    consts: dict[str, ast.Constant] = {}
    snapshots: list[dict[str, ast.Constant]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    consts[t.id] = node.value
        elif (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
              and isinstance(node.value.func, ast.Name)
              and node.value.func.id in ("test", "test_raises")
              and id(node.value) in call_ids):
            snapshots.append(dict(consts))
    return snapshots


def _imported_name_set(runner_src: str) -> set[str]:
    tree = ast.parse(runner_src)
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("implementation"):
            names.update(a.name for a in node.names if a.name != "*")
    return names


def convert_to_pytest(runner_src: str) -> str:
    """Convert the fixture runner's test(...)/test_raises(...) calls to pytest funcs."""
    tree = ast.parse(runner_src)
    # torture-11 F7: only TOP-LEVEL test()/test_raises() calls convert 1:1
    # (nested control-flow calls have no faithful standalone form). Count
    # ALL calls so silently-dropped assertions are at least LOUDLY warned —
    # check.sh stays authoritative and the [warn] in main() covers the rest.
    all_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id in ("test", "test_raises")
    ]
    calls = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Name) and fn.id in ("test", "test_raises"):
                calls.append((fn.id, node.value))
    if not calls:
        raise RuntimeError("no test(...) calls found in runner")
    if len(all_calls) > len(calls):
        print(f"warning: convert_to_pytest dropped {len(all_calls) - len(calls)} "
              "test()/test_raises() call(s) nested in control flow (only "
              "top-level calls convert 1:1); check.sh remains authoritative",
              file=sys.stderr)

    imports = _imported_names(runner_src)
    lines = ["import pytest", "from solution import " + ", ".join(imports), ""]
    imported = _imported_name_set(runner_src)
    const_snapshots = _constant_snapshots(tree, [c for _, c in calls])
    # torture-14 F5: converted tests run with ONLY solution imports — any
    # other Load name (runner-local helper/constant) is dangling. Literal
    # constants are inlined; anything else fails conversion LOUDLY (a
    # NameError-at-run-time suite would send the debug fix-loop chasing a
    # bug in the seeded test file the model cannot edit).
    builtin_names = set(dir(builtins)) | {"pytest"}
    dangling: list[str] = []

    def _local_names(node: ast.AST) -> set[str]:
        """Names BOUND inside the expression (lambda params, comprehension
        targets, walrus) — they are not dangling module names."""
        bound: set[str] = set()

        def visit(n: ast.AST) -> None:
            if isinstance(n, ast.Lambda):
                a = n.args
                for arg in (a.posonlyargs + a.args + a.kwonlyargs):
                    bound.add(arg.arg)
                if a.vararg:
                    bound.add(a.vararg.arg)
                if a.kwarg:
                    bound.add(a.kwarg.arg)
            elif isinstance(n, ast.comprehension):
                target = n.target
                if isinstance(target, ast.Name):
                    bound.add(target.id)
            elif isinstance(n, ast.NamedExpr) and isinstance(n.target, ast.Name):
                bound.add(n.target.id)
            for child in ast.iter_child_nodes(n):
                visit(child)

        visit(node)
        return {b for b in bound if b}

    class _InlineDangling(ast.NodeTransformer):
        def __init__(self, allowed: set[str], consts: dict[str, ast.Constant]) -> None:
            self.allowed = allowed
            self.consts = consts

        def visit_Name(self, node: ast.Name) -> ast.AST:
            if isinstance(node.ctx, ast.Load) and node.id not in self.allowed:
                if node.id in self.consts:
                    return ast.copy_location(self.consts[node.id], node)
                dangling.append(node.id)
            return node

    for idx, (kind, call) in enumerate(calls, start=1):
        args = call.args
        # torture-15 F10: only inline the constant values in effect at THIS
        # call site (a reassigned constant must not bleed into earlier calls).
        consts = const_snapshots[idx - 1] if idx - 1 < len(const_snapshots) else {}
        try:
            name = ast.literal_eval(args[0]) if args else f"case_{idx}"
        except (ValueError, TypeError):
            # torture-15 F11: a non-literal name arg (test_raises(EXC, ...)
            # with EXC = ValueError) must not crash with a raw traceback —
            # slug the source text as a readable case name.
            name = _slug(ast.unparse(args[0])) if args else f"case_{idx}"
        # a lambda arg means the runner calls it; otherwise it is an eager call
        is_lambda = isinstance(args[1], ast.Lambda)
        expr = args[1].body if is_lambda else args[1]
        expr = _InlineDangling(imported | builtin_names
                               | _local_names(expr), consts).visit(expr)
        expr_src = ast.unparse(expr)
        defname = f"test_{idx:02d}_{_slug(name)}"
        lines.append(f"def {defname}():")
        if kind == "test":
            expected_ast = _InlineDangling(
                imported | builtin_names
                | _local_names(args[2]), consts).visit(args[2])
            lines.append(f"    assert {expr_src} == {ast.unparse(expected_ast)}")
        else:  # test_raises
            if len(args) > 2:
                exc_ast = _InlineDangling(
                    imported | builtin_names
                    | _local_names(args[2]), consts).visit(args[2])
                exc_src = ast.unparse(exc_ast)
            else:
                exc_src = "ValueError"
            lines.append(f"    with pytest.raises({exc_src}):")
            lines.append(f"        {expr_src}")
        lines.append("")
    if dangling:
        raise RuntimeError(
            "converted tests reference names not importable from "
            f"'solution' and not runner constants: {sorted(set(dangling))} — "
            "move helpers/constants into the implementation or inline them "
            "in the runner (convert_to_pytest will not emit a suite that "
            "NameErrors at run time)")
    return "\n".join(lines)

# -------------------------------------------------------------- job seeding ---
def seed_worker(workdir: Path, solution_py: Path, test_solution_py: Path, stop: threading.Event) -> None:
    """Poll for the job dir to appear, then seed solution.py + test_solution.py."""
    deadline = time.time() + 60.0
    seeded = False
    # torture-11 F1: dirs that already exist when the seeder starts are NOT
    # this submit's job dir (a repeat run with the same RUNID — the
    # documented default — leaves them behind). Only a dir that appears
    # AFTER we start is the one to wait for.
    try:
        initial = {p.name for p in workdir.iterdir() if p.is_dir()}
    except OSError:
        initial = set()
    while not stop.is_set() and time.time() < deadline:
        try:
            entries = [p for p in workdir.iterdir() if p.is_dir()] if workdir.exists() else []
        except OSError:
            entries = []
        # seed EVERY job dir (idempotent overwrite) so old dirs can never
        # starve the new one.
        for job_dir in entries:
            try:
                if solution_py.exists():
                    shutil.copy2(solution_py, job_dir / "solution.py")
                if test_solution_py.exists():
                    shutil.copy2(test_solution_py, job_dir / "test_solution.py")
                seeded = True
            except OSError:
                pass
        # return only once the submit's NEW dir is seeded; the old code
        # returned on ANY dir with both files, so a stale dir satisfied the
        # seeder BEFORE the new dir appeared and the new run executed
        # UNSEEDED -> every fixture BLOCKed.
        new_dirs = [p for p in entries if p.name not in initial]
        if new_dirs:
            newest = max(new_dirs, key=lambda p: p.stat().st_mtime)
            if (newest / "solution.py").exists() and (newest / "test_solution.py").exists():
                return
        time.sleep(0.001)
    if not seeded:
        print(f"  [warn] seeder: job dir never appeared/seedable in {workdir}")

# ---------------------------------------------------------------- submit ----
def run_submit(fixture_dir: Path, workdir: Path, ledger_path: Path) -> tuple[dict, str, str]:
    task = (fixture_dir / "task.md").read_text(encoding="utf-8").strip()
    env = dict(os.environ)
    key = load_api_key()
    if env.get("NINE_LLM_BACKEND", "").strip().lower() in ("openai", "opencode", "rue"):
        # testing tunnel backend: the child's provider reads the NINE_LLM_*
        # chain — inject the loaded key explicitly so a bench run is honest
        # about which key/backend it used (torture-10 F6).
        env["NINE_LLM_API_KEY"] = key
    else:
        env["GEMINI_API_KEY"] = key
        env["GEMINI_MODEL"] = GEMINI_MODEL
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [str(NINE_BIN), "submit", "--workdir", str(workdir),
           "--ledger", str(ledger_path), task]
    proc = subprocess.Popen(cmd, cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    # watcher seeds the job dir the moment it appears
    stop = threading.Event()
    t = threading.Thread(target=seed_worker,
                         args=(workdir, fixture_dir / "starter" / "solution.py",
                               workdir.parent / "test_solution.py", stop),
                         daemon=True)
    t.start()
    try:
        out, err = proc.communicate(timeout=PER_FIXTURE_TIMEOUT_S)
        timed_out = False
    except subprocess.TimeoutExpired:
        # BONUS (torture-11): proc.kill() orphaned nine's DETACHED bash nodes
        # (start_new_session in the runtime) — the ghost writer kept running
        # into the abandoned job dir. Kill the whole process GROUP instead
        # (SIGTERM then SIGKILL, same pattern as the runtime).
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=3.0)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        # torture-13 F2: killpg(proc.pid) reaches only the nine CLI — the
        # runtime spawns every bash node in its OWN process group
        # (start_new_session), and the CLI's own node-timeout cleanup dies
        # with it. Kill the recorded node groups too, or the detached pytest
        # tree keeps writing into the abandoned job dir.
        _kill_node_groups(workdir)
        out, err = proc.communicate()
        timed_out = True
    finally:
        stop.set()
        t.join(timeout=2.0)
    return parse_submit_output(out, err, timed_out), out, err


def _node_start_epoch(pid: int) -> float | None:
    """Wall-clock start time of a live process (best-effort, portable).

    torture-15 F9: identity validation for the pid-file killer. Linux uses
    /proc/<pid>/stat starttime + /proc/stat btime (robust against pid
    reuse); other platforms fall back to `ps -o lstart=` (macOS, BSD).
    Returns None when the process is gone or the source is unavailable —
    the caller must then be CONSERVATIVE (do not kill).
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            after_comm = fh.read().rsplit(")", 1)[1].split()
        start_ticks = int(after_comm[19])  # field 22, idx 19 after comm
        with open("/proc/stat", encoding="utf-8") as fh:
            btime = None
            for line in fh:
                if line.startswith("btime "):
                    btime = int(line.split()[1])
                    break
        if btime is None:
            return None
        clk = os.sysconf("SC_CLK_TCK")
        return btime + start_ticks / clk
    except (OSError, IndexError, ValueError):
        pass
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2.0, check=False,
        ).stdout.strip()
        if not out:
            return None
        return datetime.datetime.strptime(
            out, "%a %b %d %H:%M:%S %Y").timestamp()
    except Exception:  # noqa: BLE001 - best-effort identity check
        return None


def _kill_node_groups(workdir: Path) -> int:
    """SIGTERM->SIGKILL every runtime bash-node process group recorded in
    the fixture run dir (torture-13 F2).

    The runtime appends each detached bash node's group-leader pid +
    spawn wall-clock time to .nine-node-pids in the job dir (never a
    manifest entry). After a per-fixture timeout kills the nine CLI, these
    groups are orphaned — read the pid files and kill each group.
    torture-15 F9: only kill a pid that is (a) a session leader (the
    runtime's bash nodes are start_new_session=True) AND (b) still the
    SAME process as recorded (spawn time matches) — a recycled pid must
    never SIGKILL an innocent process group. Stale pids whose start time
    can no longer be verified are skipped conservatively.
    """
    killed = 0
    for pid_file in workdir.rglob(".nine-node-pids"):
        try:
            entries: list[tuple[int, float | None]] = []
            for line in pid_file.read_text().splitlines():
                parts = line.strip().split()
                if not parts or not parts[0].isdigit():
                    continue  # garbage line: skip
                pid = int(parts[0])
                start = float(parts[1]) if len(parts) > 1 else None
                entries.append((pid, start))
        except OSError:
            continue
        for pid, start in entries:
            try:
                # identity gate 1: must still be a session leader (the
                # runtime spawns every bash node start_new_session=True)
                if os.getsid(pid) != pid:
                    continue
                # identity gate 2: recorded spawn time must match the live
                # process — a recycled pid (node exited, OS reused the
                # number) has a different start. When we cannot verify
                # (process already gone / no source), be conservative.
                if start is not None:
                    actual = _node_start_epoch(pid)
                    if actual is None or abs(actual - start) > 3.0:
                        continue
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                continue
            try:
                time.sleep(0.2)
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            killed += 1
    return killed

def parse_submit_output(stdout: str, stderr: str, timed_out: bool) -> dict:
    info = {
        "timed_out": timed_out,
        "workflow_id": None,
        "router_model": None,
        "verdict": None,
        "verdict_summary": None,
        "job_id": None,
        "final_status": None,
        "cli_error": None,
    }
    m = re.search(r'"workflow_id":\s*"([^"]+)"', stdout)
    if m:
        info["workflow_id"] = m.group(1)
    m = re.search(r'"model":\s*"([^"]+)"', stdout)
    if m:
        info["router_model"] = m.group(1)
    m = re.search(r"\[verdict\]\s+(\w+)\s*-\s*(.*)", stdout)
    if m:
        info["verdict"] = m.group(1)
        info["verdict_summary"] = m.group(2).strip()
    m = re.search(r"\[job\]\s+(\S+)\s+->\s+(\S+)", stdout)
    if m:
        info["job_id"] = m.group(1)
        info["final_status"] = m.group(2)
    m = re.search(r"\[error\]\s+(.*)", stderr)
    if m:
        info["cli_error"] = m.group(1).strip()
    return info

# ------------------------------------------------------------- independent ----
def verify_with_check_sh(check_sh: Path, patch_py: Path) -> dict:
    """Run the fixture's own check.sh against the produced patch.py."""
    if not patch_py.exists():
        return {"tests_passed": 0, "tests_total": 0, "exit_code": None, "ran": False, "detail": "patch.py missing"}
    try:
        r = subprocess.run(["bash", str(check_sh), str(patch_py)],
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        # torture-11 F4: one hanging candidate patch must NOT abort the
        # whole bench before results.json is written.
        return {"tests_passed": 0, "tests_total": 0, "exit_code": None,
                "ran": False, "timed_out": True,
                "detail": "check.sh timed out (120s): "
                          f"{(exc.stdout or b'')[-400:]!r}"}
    passed = len(re.findall(r"✅ PASS", r.stdout))
    failed = len(re.findall(r"❌ FAIL", r.stdout))
    return {
        "tests_passed": passed,
        "tests_total": passed + failed,
        "exit_code": r.returncode,
        "ran": True,
        "detail": (r.stdout + r.stderr)[-800:],
    }

def attempts_from_ledger(ledger_path: Path, job_id: str | None) -> int | None:
    if not job_id or not ledger_path.exists():
        return None
    attempts = None
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("job_id") == job_id:
            attempts = rec.get("attempts")
    return attempts

# ------------------------------------------------------------------ main -----
def main() -> int:
    BENCH_ROOT.mkdir(parents=True, exist_ok=True)
    results = []
    for fx in FIXTURES:
        fx_dir = FIXTURES_DIR / fx
        if not fx_dir.exists():
            print(f"[skip] {fx}: fixture dir missing")
            continue
        fx_root = BENCH_ROOT / fx / f"run-{RUNID}"
        # torture-11 F1: a repeat run with the same RUNID (the documented
        # default) must start CLEAN — leftover job dirs from the previous
        # run made the seeder return early and the new run execute
        # UNSEEDED -> every fixture BLOCKed. Fresh evidence per run.
        if fx_root.exists():
            shutil.rmtree(fx_root)
        workdir = fx_root / "work"
        workdir.mkdir(parents=True, exist_ok=True)
        ledger = fx_root / "ledger.jsonl"

        # convert the fixture's own tests into pytest test_solution.py (for the
        # debug lane's verify node); the independent check uses check.sh itself
        try:
            runner_src = extract_runner(fx_dir / "tests" / "check.sh")
            pytest_src = convert_to_pytest(runner_src)
            (fx_root / "test_solution.py").write_text(pytest_src, encoding="utf-8")
            expected_tests = pytest_src.count("\ndef test_")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {fx}: test conversion failed: {exc}; continuing with check.sh only")
            expected_tests = None

        task_md = (fx_dir / "task.md").read_text(encoding="utf-8")
        task = task_from_md(task_md, TASK_MODE)
        print(f"\n=== {fx} [task_mode={TASK_MODE}] ===")
        preview = next((ln.strip() for ln in task.splitlines() if ln.strip()), task[:80])
        print(f"  task: {preview[:100]!r}")

        t0 = time.monotonic()
        info, stdout, stderr = run_submit(fx_dir, workdir, ledger)
        duration_s = round(time.monotonic() - t0, 1)

        if not info["job_id"] and workdir.exists():
            # failed-loud jobs print no [job] line; recover the newest job dir
            dirs = sorted([p for p in workdir.iterdir() if p.is_dir()],
                          key=lambda p: p.stat().st_mtime, reverse=True)
            if dirs:
                info["job_id"] = dirs[0].name
                info["final_status"] = info["final_status"] or "failed-loud"
        if info["cli_error"] and not info["verdict"]:
            info["verdict"] = "ERROR"
        job_dir = (workdir / info["job_id"]) if info["job_id"] else None
        candidate = None
        candidate_file = None
        if job_dir is not None:
            if (job_dir / "patch.py").exists():
                candidate, candidate_file = job_dir / "patch.py", "patch.py"
            elif (job_dir / "solution.py").exists():
                candidate, candidate_file = job_dir / "solution.py", "solution.py"
        starter = fx_dir / "starter" / "solution.py"
        candidate_unchanged = bool(candidate and starter.exists()
                                   and candidate.read_bytes() == starter.read_bytes())
        if candidate and not candidate_unchanged:
            verdict_res = verify_with_check_sh(fx_dir / "tests" / "check.sh", candidate)
        elif candidate and candidate_unchanged and info["verdict"] != "SHIP":
            # torture-11 F8: a BLOCKed fixture still carries the SEEDED broken
            # starter as its "candidate" — scoring it inflates the tests
            # column with failures the run never produced (and misleads
            # 'pick the worst fixture'). Don't score; flag it instead.
            verdict_res = {
                "tests_passed": 0, "tests_total": 0, "exit_code": None,
                "ran": False, "detail": "candidate unchanged from broken starter (no fix produced)"}
        else:
            verdict_res = {
                "tests_passed": 0, "tests_total": 0, "exit_code": None, "ran": False,
                "detail": "no job dir / produced solution file"}
        attempts = attempts_from_ledger(ledger, info["job_id"])

        rec = {
            "fixture": fx,
            "routed_workflow": info["workflow_id"],
            "router_model": info["router_model"],
            "verdict": info["verdict"],
            "verdict_summary": info["verdict_summary"],
            "final_status": info["final_status"],
            "attempts": attempts,
            "duration_s": duration_s,
            "tests_passed": verdict_res["tests_passed"],
            "tests_total": verdict_res["tests_total"],
            "check_exit": verdict_res.get("exit_code"),
            "timed_out": info["timed_out"],
            "cli_error": info["cli_error"],
            "job_id": info["job_id"],
            "task_mode": TASK_MODE,
            "run_id": RUNID,
            "test_solution_expected_tests": expected_tests,
            "candidate_file": candidate_file,
            "candidate_unchanged_from_starter": candidate_unchanged,
        }
        results.append(rec)
        print(f"  workflow={rec['routed_workflow']} verdict={rec['verdict']} "
              f"status={rec['final_status']} attempts={rec['attempts']} "
              f"tests={rec['tests_passed']}/{rec['tests_total']} time={duration_s}s")
        if rec["cli_error"]:
            print(f"  cli_error: {rec['cli_error']}")

    (BENCH_ROOT / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    (BENCH_ROOT / f"results-{TASK_MODE}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    # torture-11 F6: keep a stable per-run scorecard (results.json is the
    # live file, overwritten by the NEXT run) — regression archives.
    # torture-13 F5: the documented default invocation (RUNID="r0") must
    # archive too, or every default run silently destroys the previous
    # scoreboard with no comparison source.
    if RUNID:
        shutil.copy(BENCH_ROOT / "results.json",
                    BENCH_ROOT / f"results-{RUNID}.json")
    print("\n=== RESULTS ===")
    print(f"{'fixture':<18}{'workflow':<12}{'verdict':<8}{'tests':<9}{'time_s':<8}{'attempts'}")
    for r in results:
        tests_str = f"{r['tests_passed']}/{r['tests_total']}"
        print(f"{r['fixture']:<18}{str(r['routed_workflow']):<12}{str(r['verdict']):<8}"
              f"{tests_str:<9}{r['duration_s']:<8}{r['attempts']}")
    shipped = sum(1 for r in results if r["verdict"] == "SHIP")
    total_t = sum(r["tests_passed"] for r in results)
    total_tt = sum(r["tests_total"] for r in results)
    if results and shipped == len(results):
        print(f"\nSCORE: SHIP {shipped}/{len(results)}  tests {total_t}/{total_tt}  PASS")
        return 0
    # torture-11 F6: a non-full SHIP is a FAILING bench — exit non-zero so
    # automation can key on the score (0/9 was indistinguishable from 9/9).
    fails = [r["fixture"] for r in results if r["verdict"] != "SHIP"]
    print(f"\nSCORE: SHIP {shipped}/{len(results)}  tests {total_t}/{total_tt}  "
          f"FAIL (non-SHIP: {fails})")
    return 1

if __name__ == "__main__":
    sys.exit(main())
