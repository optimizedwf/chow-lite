"""Deploy-check workflow - pre-deploy readiness: env + validate + risk-review.

The `deploy-check` lane of nine: preflight (bash) inspects the workspace,
env-scan (bash) maps env/config requirements, validate (bash) runs the
tests and writes EVAL.json, risk (prompt) reads the evidence and writes
RISK.md, and decision (prompt) writes DEPLOY_CHECK.md with an explicit
`Decision: GO|NO-GO` line. Gate requires the Decision line + passing EVAL.

Model-or-fail: without GEMINI_API_KEY the prompt nodes raise WorkflowError -
the job fails loud. NEVER a canned readiness verdict.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nine.chains.chain import Hop
from nine.gates.evidence import (
    eval_json_check,
    exit_codes_check,
    required_artifact_check,
)
from nine.runtime.summarizer import _gemini_generate
from nine.runtime.workflows import Node, Workflow, WorkflowError


def _preflight_command() -> str:
    """Bash node: verify the deployable artifact exists and is runnable.

    Writes PREFLIGHT.md: entrypoint found (solution.py / solution/main.py),
    config files present, python version. Always exits 0; evidence is the
    artifact.
    """
    return r"""
echo '# Preflight' > PREFLIGHT.md
python3 --version >> PREFLIGHT.md
if [ -f solution.py ]; then
  echo 'entrypoint: solution.py (top-level)' >> PREFLIGHT.md
elif [ -f solution/main.py ]; then
  echo 'entrypoint: solution/main.py' >> PREFLIGHT.md
else
  echo 'entrypoint: NONE FOUND' >> PREFLIGHT.md
fi
echo '## Config Files' >> PREFLIGHT.md
for f in requirements.txt pyproject.toml package.json .env.example Dockerfile Procfile; do
  if [ -f "$f" ]; then echo "- $f (present)" >> PREFLIGHT.md; else echo "- $f (missing)" >> PREFLIGHT.md; fi
done
exit 0
"""


def _env_scan_command() -> str:
    """Bash node: map environment/config requirements from the code.

    Writes ENV_SCAN.md: os.environ / os.getenv usages (with the var name),
    .env files found, hard-coded secrets candidates. Always exits 0.
    """
    return r"""
echo '# Environment Scan' > ENV_SCAN.md
echo '## os.environ / os.getenv references' >> ENV_SCAN.md
grep -rnoE "os\.(environ|getenv)\s*\(?\s*[\"'][A-Z_]+[\"']" --include='*.py' . 2>/dev/null | grep -v '.venv' | grep -v '__pycache__' | head -40 >> ENV_SCAN.md || true
echo '## .env files' >> ENV_SCAN.md
find . -name '.env*' -not -path './.venv/*' -not -path '*/__pycache__/*' >> ENV_SCAN.md || true
echo '## Secrets candidates (API_KEY, TOKEN, PASSWORD, SECRET)' >> ENV_SCAN.md
grep -rn 'API_KEY\|TOKEN\|PASSWORD\|SECRET' --include='*.py' . 2>/dev/null | grep -v '.venv' | grep -v '__pycache__' | head -20 >> ENV_SCAN.md || true
exit 0
"""


def _validate_command() -> str:
    """Bash node: run the tests (or the entrypoint) and write EVAL.json.

    Mirrors the verify node of other lanes: test_solution.py -> pytest,
    solution/test_*.py -> pytest in package, else run entrypoint. Always
    exits 0 so the gate decides SHIP/FIX from the evidence.
    """
    return r"""
if [ -f test_solution.py ]; then
  PYTHONPATH=. python3 -B -m pytest test_solution.py --tb=short -q > deploy_test.log 2>&1; rc=$?
elif ls solution/test_*.py >/dev/null 2>&1; then
  PYTHONPATH=solution python3 -B -m pytest solution/test_*.py --tb=short -q > deploy_test.log 2>&1; rc=$?
elif [ -f solution.py ]; then
  python3 -B solution.py > deploy_run.log 2>&1; rc=$?
elif [ -f solution/main.py ]; then
  PYTHONPATH=. python3 -B solution/main.py > deploy_run.log 2>&1; rc=$?
else
  rc=2
fi
if [ $rc -eq 0 ]; then
  printf '{"checks":[{"name":"deploy-validate","passed":true,"message":"tests/entrypoint exit 0"}],"exit_code":0}' > EVAL.json
else
  printf '{"checks":[{"name":"deploy-validate","passed":false,"message":"exit %s"}],"exit_code":%s}' "$rc" "$rc" > EVAL.json
fi
exit 0
"""


def _risk_prompt_node() -> Node:
    """Prompt node: read evidence, write RISK.md (deploy risks + mitigations)."""
    def _run(inputs: dict, job_dir) -> dict:
        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:300]

        def _read(name: str, limit: int = 2500) -> str:
            p = job_dir / name
            return p.read_text(encoding="utf-8")[:limit] if p.exists() else "(missing)"

        prompt = (
            "You are the deploy risk reviewer of nine, an evidence-gated "
            "agent OS. Read the preflight, environment scan and validation "
            "evidence, then write RISK.md with: 1) Top Risks (numbered, "
            "each with severity HIGH/MED/LOW and one mitigation), "
            "2) a one-line Risk Summary. Do not invent requirements that "
            "are not in the evidence.\n"
            f"Task: {task}\n"
            f"PREFLIGHT.md:\n{_read('PREFLIGHT.md')}\n"
            f"ENV_SCAN.md:\n{_read('ENV_SCAN.md')}\n"
            f"EVAL.json:\n{_read('EVAL.json')}\n"
        )
        risk = _gemini_generate(prompt, api_key=None)
        if not (risk and risk.strip()):
            raise WorkflowError(
                "deploy-check (risk) model returned no RISK.md - job failed "
                "loud (no offline fallback)"
            )
        (job_dir / "RISK.md").write_text(risk.strip(), encoding="utf-8")
        return {"output": "wrote RISK.md", "artifact_path": str(job_dir / "RISK.md")}

    return Node(
        id="risk", kind="prompt", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="Prompt node writes RISK.md (fails loud without a model)",
    )


def _decision_prompt_node() -> Node:
    """Prompt node: write DEPLOY_CHECK.md with an explicit Decision line."""
    def _run(inputs: dict, job_dir) -> dict:
        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:300]

        def _read(name: str, limit: int = 2500) -> str:
            p = job_dir / name
            return p.read_text(encoding="utf-8")[:limit] if p.exists() else "(missing)"

        prompt = (
            "You are the deploy decision gate of nine, an evidence-gated "
            "agent OS. Based ONLY on the evidence below, write "
            "DEPLOY_CHECK.md: 1) a line exactly `Decision: GO` or "
            "`Decision: NO-GO`, 2) a short Justification (3-5 bullets tied "
            "to evidence), 3) Required Actions if any. If validation "
            "failed or a HIGH risk is unremediated, decide NO-GO.\n"
            f"Task: {task}\n"
            f"RISK.md:\n{_read('RISK.md')}\n"
            f"EVAL.json:\n{_read('EVAL.json')}\n"
        )
        decision = _gemini_generate(prompt, api_key=None)
        if not (decision and decision.strip()):
            raise WorkflowError(
                "deploy-check (decision) model returned no DEPLOY_CHECK.md - "
                "job failed loud (no offline fallback)"
            )
        (job_dir / "DEPLOY_CHECK.md").write_text(decision.strip(), encoding="utf-8")
        return {"output": "wrote DEPLOY_CHECK.md",
                "artifact_path": str(job_dir / "DEPLOY_CHECK.md")}

    return Node(
        id="decision", kind="prompt", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="Prompt node writes DEPLOY_CHECK.md with Decision line (fails loud without a model)",
    )


def _decision_line_check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """DEPLOY_CHECK.md must carry an explicit `Decision:` line (GO or NO-GO)."""
    p = Path(workdir) / "DEPLOY_CHECK.md"
    if not p.exists():
        return False, "DEPLOY_CHECK.md missing"
    txt = p.read_text(encoding="utf-8")
    if "Decision:" not in txt:
        return False, "DEPLOY_CHECK.md missing Decision line"
    if "GO" not in txt.split("Decision:")[1][:40]:
        return False, "Decision line must read GO or NO-GO"
    return True, "DEPLOY_CHECK.md has Decision"


def deploy_check_hop() -> Hop:
    """The `deploy-check` workflow: pre-deploy readiness.

    Five-node hop:
      1. preflight (bash)   - PREFLIGHT.md (entrypoint + config presence)
      2. env-scan (bash)    - ENV_SCAN.md (env refs, .env files, secret candidates)
      3. validate (bash)    - runs tests/entrypoint, writes EVAL.json
      4. risk (prompt)      - RISK.md (risks + mitigations from evidence)
      5. decision (prompt)  - DEPLOY_CHECK.md with Decision: GO|NO-GO

    Gate: EVAL passed + Decision line present + all five artifacts.
    """
    wf = Workflow(id="deploy-check",
                  description="Pre-deploy readiness: env + validate + risk-review")
    preflight = Node(id="preflight", kind="bash", command=_preflight_command(),
                     description="Write PREFLIGHT.md (entrypoint + config)")
    env_scan = Node(id="env-scan", kind="bash", command=_env_scan_command(),
                    description="Write ENV_SCAN.md (env refs + secrets candidates)")
    validate = Node(id="validate", kind="bash", command=_validate_command(),
                    description="Run tests/entrypoint, write EVAL.json")
    risk = _risk_prompt_node()
    risk.depends_on = ["preflight", "env-scan", "validate"]
    decision = _decision_prompt_node()
    decision.depends_on = ["risk"]
    for n in (preflight, env_scan, validate, risk, decision):
        wf.add_node(n)
    return Hop(
        id="deploy-check", workflow=wf,
        required_artifacts=["PREFLIGHT.md", "ENV_SCAN.md", "EVAL.json",
                            "RISK.md", "DEPLOY_CHECK.md"],
        gate_checks={
            "eval-json": eval_json_check(),
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(
                ["PREFLIGHT.md", "ENV_SCAN.md", "EVAL.json",
                 "RISK.md", "DEPLOY_CHECK.md"]
            ),
            "decision-line": _decision_line_check,
        },
        max_fix_loops=2,
    )
