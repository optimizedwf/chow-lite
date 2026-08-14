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
import json
import os
import re
import shutil
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

def convert_to_pytest(runner_src: str) -> str:
    """Convert the fixture runner's test(...)/test_raises(...) calls to pytest funcs."""
    tree = ast.parse(runner_src)
    calls = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Name) and fn.id in ("test", "test_raises"):
                calls.append((fn.id, node.value))
    if not calls:
        raise RuntimeError("no test(...) calls found in runner")

    imports = _imported_names(runner_src)
    lines = ["import pytest", "from solution import " + ", ".join(imports), ""]
    for idx, (kind, call) in enumerate(calls, start=1):
        args = call.args
        name = ast.literal_eval(args[0]) if args else f"case_{idx}"
        expr_src = ast.unparse(args[1])
        # a lambda arg means the runner calls it; otherwise it is an eager call
        is_lambda = isinstance(args[1], ast.Lambda)
        if is_lambda:
            expr_src = ast.unparse(args[1].body)  # strip the lambda wrapper
        defname = f"test_{idx:02d}_{_slug(name)}"
        lines.append(f"def {defname}():")
        if kind == "test":
            expected = ast.unparse(args[2])
            lines.append(f"    assert {expr_src} == {expected}")
        else:  # test_raises
            exc = ast.unparse(args[2]) if len(args) > 2 else "ValueError"
            lines.append(f"    with pytest.raises({exc}):")
            lines.append(f"        {expr_src}")
        lines.append("")
    return "\n".join(lines)

# -------------------------------------------------------------- job seeding ---
def seed_worker(workdir: Path, solution_py: Path, test_solution_py: Path, stop: threading.Event) -> None:
    """Poll for the job dir to appear, then seed solution.py + test_solution.py."""
    deadline = time.time() + 60.0
    seeded = False
    while not stop.is_set() and time.time() < deadline:
        try:
            entries = [p for p in workdir.iterdir() if p.is_dir()] if workdir.exists() else []
        except OSError:
            entries = []
        for job_dir in entries:
            try:
                if not seeded:
                    if solution_py.exists():
                        shutil.copy2(solution_py, job_dir / "solution.py")
                    if test_solution_py.exists():
                        shutil.copy2(test_solution_py, job_dir / "test_solution.py")
                    seeded = True
                if (job_dir / "solution.py").exists() and (job_dir / "test_solution.py").exists():
                    return
            except OSError:
                pass
        time.sleep(0.001)
    if not seeded:
        print(f"  [warn] seeder: job dir never appeared/seedable in {workdir}")

# ---------------------------------------------------------------- submit ----
def run_submit(fixture_dir: Path, workdir: Path, ledger_path: Path) -> tuple[dict, str, str]:
    task = (fixture_dir / "task.md").read_text(encoding="utf-8").strip()
    env = dict(os.environ)
    env["GEMINI_API_KEY"] = load_api_key()
    env["GEMINI_MODEL"] = GEMINI_MODEL
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [str(NINE_BIN), "submit", "--workdir", str(workdir),
           "--ledger", str(ledger_path), task]
    proc = subprocess.Popen(cmd, cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
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
        proc.kill()
        out, err = proc.communicate()
        timed_out = True
    finally:
        stop.set()
        t.join(timeout=2.0)
    return parse_submit_output(out, err, timed_out), out, err

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
    r = subprocess.run(["bash", str(check_sh), str(patch_py)],
                       capture_output=True, text=True, timeout=120)
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
        verdict_res = verify_with_check_sh(fx_dir / "tests" / "check.sh", candidate) if candidate else {
            "tests_passed": 0, "tests_total": 0, "exit_code": None, "ran": False,
            "detail": "no job dir / produced solution file"}
        starter = fx_dir / "starter" / "solution.py"
        candidate_unchanged = bool(candidate and starter.exists()
                                   and candidate.read_bytes() == starter.read_bytes())
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
    print("\n=== RESULTS ===")
    print(f"{'fixture':<18}{'workflow':<12}{'verdict':<8}{'tests':<9}{'time_s':<8}{'attempts'}")
    for r in results:
        tests_str = f"{r['tests_passed']}/{r['tests_total']}"
        print(f"{r['fixture']:<18}{str(r['routed_workflow']):<12}{str(r['verdict']):<8}"
              f"{tests_str:<9}{r['duration_s']:<8}{r['attempts']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
