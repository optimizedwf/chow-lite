"""Verify workflow - audit a workspace against a task's claims (the cops).

The `verify` lane of nine (Lane I - Security/Proof, "The Auditor"):

  collect  (bash)  - inventory the job's outputs (files, sizes, the primary
                     document, EVAL.json gate evidence) -> VERIFY_INVENTORY.md
  claims   (model) - extract the explicit checkable claims the work claims to
                     satisfy -> CLAIMS.md (numbered, one claim per line)
  check    (bash)  - deterministic mechanical checks per claim (referenced
                     files exist + compile, test evidence in EVAL.json)
                     -> CHECKS.json (machine) + CHECKS.md (human)
  verdict  (model) - honest per-claim statuses + overall verdict
                     -> VERIFIED.json

The gate is HONESTY-ENFORCING (the cop that audits the cops): every
mechanical FAIL in CHECKS.json must be reported FAIL in VERIFIED.json; the
report verdict must follow the evidence (any FAIL -> UNVERIFIED, any
UNCHECKED -> PARTIAL, all PASS -> VERIFIED); and every extracted claim must
appear exactly once. The lane SHIPs when the AUDIT is complete and honest -
the inner verdict field carries the outcome, exactly like a review SHIPs a
review that contains criticisms.

Model-or-fail: without an LLM key the model nodes raise WorkflowError - the
job fails loud. NEVER a canned audit.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from nine.chains.chain import Hop
from nine.gates.evidence import (
    exit_codes_check,
    file_nonempty_check,
    required_artifact_check,
)
from nine.runtime.llm_provider import key_available
from nine.runtime.summarizer import _gemini_generate
from nine.runtime.workflows import Node, Workflow, WorkflowError

_VERDICTS = ("VERIFIED", "PARTIAL", "UNVERIFIED")
_STATUSES = ("PASS", "FAIL", "UNCHECKED")


def _require_key(lane: str) -> None:
    """Model-or-fail: every model node checks the provider key first."""
    if not key_available():
        raise WorkflowError(
            f"{lane} requires an LLM key (gemini: GEMINI_API_KEY; openai: "
            "NINE_LLM_API_KEY/OPENCODE_GO_API_KEY) - no offline fallback, "
            "nine is model-driven"
        )


def _collect_command() -> str:
    """Bash node: inventory the job's outputs -> VERIFY_INVENTORY.md."""
    return r"""
echo '# Verify Inventory' > VERIFY_INVENTORY.md
echo '' >> VERIFY_INVENTORY.md
echo '## Files' >> VERIFY_INVENTORY.md
find . -type f \
  -not -path '*/__pycache__/*' \
  -not -name 'VERIFY_INVENTORY.md' \
  -not -name 'CLAIMS.md' \
  -not -name 'CHECKS.md' \
  -not -name 'CHECKS.json' \
  -not -name 'VERIFIED.json' \
  -not -name 'task.txt' \
  | sort | while read -r f; do
    sz=$(stat -f '%z' "$f" 2>/dev/null || stat -c '%s' "$f" 2>/dev/null)
    echo "- $f (${sz:-?} bytes)" >> VERIFY_INVENTORY.md
  done
echo '' >> VERIFY_INVENTORY.md
echo '## Primary document' >> VERIFY_INVENTORY.md
for f in REPORT.md README.md RESULT.md solution/README.md REPORT.txt; do
  if [ -f "$f" ]; then
    echo "### $f" >> VERIFY_INVENTORY.md
    head -c 6000 "$f" >> VERIFY_INVENTORY.md
    echo '' >> VERIFY_INVENTORY.md
    break
  fi
done
if [ -f EVAL.json ]; then
  echo '' >> VERIFY_INVENTORY.md
  echo '## EVAL.json (gate evidence)' >> VERIFY_INVENTORY.md
  cat EVAL.json >> VERIFY_INVENTORY.md
  echo '' >> VERIFY_INVENTORY.md
fi
exit 0
"""


def _claims_prompt_node() -> Node:
    """Prompt node: extract explicit checkable claims -> CLAIMS.md."""
    def _run(inputs: dict, job_dir) -> dict:
        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:500]
        _require_key("verify (claims)")

        inventory = ""
        p = job_dir / "VERIFY_INVENTORY.md"
        if p.exists():
            inventory = p.read_text(encoding="utf-8")[:6000]

        prompt = (
            "You are the claims extractor of nine's verify lane (the cop "
            "that audits agent output). Read the task and the workspace "
            "inventory, then write CLAIMS.md listing the EXPLICIT, "
            "CHECKABLE claims the work must satisfy.\n"
            "Rules:\n"
            "1. One claim per line, numbered: `1. <claim>` , `2. <claim>` ...\n"
            "2. Prefer mechanically checkable claims (a file exists, a "
            "function is defined, a test passes, an output string is "
            "present).\n"
            "3. Do not invent claims the task does not state. 3-10 claims "
            "is normal.\n"
            "4. Output ONLY the numbered claim lines - no preamble, no "
            "summary, no markdown fence.\n"
            f"Task: {task}\n"
            f"VERIFY_INVENTORY.md:\n{inventory}\n"
        )
        claims = _gemini_generate(prompt, api_key=None)
        if not (claims and claims.strip()):
            raise WorkflowError(
                "verify (claims) model returned nothing - job failed loud "
                "(no offline fallback)"
            )
        (job_dir / "CLAIMS.md").write_text(
            "# Claims\n\n" + claims.strip() + "\n", encoding="utf-8"
        )
        return {"output": "wrote CLAIMS.md",
                "artifact_path": str(job_dir / "CLAIMS.md")}

    return Node(
        id="claims", kind="prompt", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="Extract explicit checkable claims into CLAIMS.md",
    )


def _check_command() -> str:
    """Bash node: deterministic mechanical checks -> CHECKS.json + CHECKS.md."""
    return r"""
python - <<'PYCHECK'
import json, re, subprocess, sys
from pathlib import Path

# torture-24 F1: exists() is True for a FIFO/device/socket, so a hostile or
# accidental hop that `mkfifo EVAL.json` (or CLAIMS.md, or any claimed ref)
# would make read_text()/stat() below BLOCK the whole check node for the
# full NINE_NODE_TIMEOUT_S. is_file() is False for non-regular files, so a
# FIFO degrades to a fast, honest FAIL instead of a hang.
def _regular(p):
    try:
        return p.is_file()
    except OSError:
        return False

def _safe_out(p):
    # write-side guard: a pre-existing FIFO at CHECKS.json/CHECKS.md would
    # block open(..., "w") forever -- refuse loudly instead (exit 2 -> node
    # FAIL, visible to the model, no 300s hang).
    if p.exists() and not p.is_file():
        sys.stderr.write(f"refusing to write {p}: not a regular file\n")
        sys.exit(2)

claims = []
cm = Path("CLAIMS.md")
if _regular(cm):
    for line in cm.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*(\d+)[.)]\s+(.+)$", line)
        if m:
            claims.append({"n": int(m.group(1)), "claim": m.group(2).strip()})

results = []
for c in claims:
    text = c["claim"]
    # candidate file refs: backticked paths, or path-shaped tokens
    refs = re.findall(r"`([^`]+)`", text)
    refs += [t for t in re.findall(r"\b([\w./-]+\.py)\b", text)
             if t not in refs]
    files = []
    for r in refs:
        r = r.strip()
        if not r or r in files:
            continue
        if "/" in r or r.endswith((".py", ".md", ".json", ".txt",
                                   ".toml", ".cfg", ".sh", ".yaml", ".yml")):
            files.append(r)

    ev = []
    status = "PASS"
    tested = False
    if files:
        existing = [f for f in files if _regular(Path(f))]
        missing = [f for f in files if not _regular(Path(f))]
        if missing:
            status = "FAIL"
            ev.append("missing referenced file(s): " + ", ".join(missing))
        for f in existing:
            ev.append(f"{f} exists ({Path(f).stat().st_size} bytes)")
            if f.endswith(".py"):
                rc = subprocess.run(
                    [sys.executable, "-m", "py_compile", f],
                    capture_output=True, text=True,
                )
                if rc.returncode != 0:
                    status = "FAIL"
                    ev.append(f"{f} does not compile: "
                              + (rc.stderr or rc.stdout or "").strip()[:200])
                else:
                    ev.append(f"{f} compiles")

    ej = Path("EVAL.json")
    if _regular(ej):
        try:
            d = json.loads(ej.read_text(encoding="utf-8"))
            passed = int(d.get("passed", -1))
            failed = int(d.get("failed", -1))
        except Exception:
            passed, failed = -1, -1
        if re.search(r"test", text, re.I) and passed >= 0:
            tested = True
            if failed and failed > 0:
                status = "FAIL"
                ev.append(f"EVAL.json reports {failed} failing test(s)")
            else:
                ev.append(f"EVAL.json reports {passed} passed / "
                          f"{failed} failed")

    if not files and not tested:
        status = "UNCHECKED"
        ev.append("no mechanically checkable file/test reference")
    results.append({
        "n": c["n"],
        "claim": c["claim"],
        "status": status,
        "evidence": "; ".join(ev) or "(no mechanical evidence)",
        "mechanical": bool(files or tested),
    })

_safe_out(Path("CHECKS.json"))
Path("CHECKS.json").write_text(
    json.dumps({"claim_count": len(claims), "claims": results}, indent=2),
    encoding="utf-8",
)
_safe_out(Path("CHECKS.md"))
with open("CHECKS.md", "w", encoding="utf-8") as fh:
    fh.write("# Checks\n\n")
    fh.write(f"Claims extracted: {len(claims)}\n\n")
    fh.write("| # | status | claim | evidence |\n|---|---|---|---|\n")
    for r in results:
        fh.write(f"| {r['n']} | {r['status']} | "
                 f"{r['claim'].replace('|', '\\|')} | "
                 f"{r['evidence'].replace('|', '\\|')} |\n")
sys.exit(0)
PYCHECK
"""


def _verdict_prompt_node() -> Node:
    """Prompt node: honest per-claim statuses + verdict -> VERIFIED.json."""
    def _run(inputs: dict, job_dir) -> dict:
        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:500]
        _require_key("verify (verdict)")

        def _read(name: str, limit: int = 6000) -> str:
            p = job_dir / name
            return p.read_text(encoding="utf-8")[:limit] if p.exists() else "(missing)"

        checks_json = _read("CHECKS.json")
        prompt = (
            "You are the verdict cop of nine's verify lane. Below are the "
            "extracted claims and the DETERMINISTIC mechanical check results "
            "(CHECKS.json). For every claim assign a final status and write "
            "VERIFIED.json.\n"
            "Rules (the gate enforces these - do not violate them):\n"
            "1. A claim whose mechanical status is FAIL must keep status "
            "FAIL. Never upgrade a mechanical FAIL to PASS.\n"
            "2. You MAY downgrade a mechanical PASS to FAIL if you see a "
            "real semantic problem in the workspace evidence - with the "
            "specific reason as evidence.\n"
            "3. A claim with mechanical status UNCHECKED stays UNCHECKED "
            "unless you produce concrete evidence from the workspace that "
            "proves or refutes it.\n"
            "4. Overall verdict must follow the evidence EXACTLY: any FAIL "
            "-> UNVERIFIED; else any UNCHECKED -> PARTIAL; else all PASS -> "
            "VERIFIED.\n"
            "5. Output ONLY a JSON object, no markdown fence, exactly:\n"
            '{"verdict": "VERIFIED|PARTIAL|UNVERIFIED", "summary": "<one '
            'line>", "claims": [{"claim": "<exact claim text>", "status": '
            '"PASS|FAIL|UNCHECKED", "evidence": "<what you verified>"}]}\n'
            "Include EVERY extracted claim exactly once, in order.\n"
            f"Task: {task}\n"
            f"CHECKS.json:\n{checks_json}\n"
        )
        text = _gemini_generate(prompt, api_key=None)
        if not (text and text.strip()):
            raise WorkflowError(
                "verify (verdict) model returned nothing - job failed loud "
                "(no offline fallback)"
            )
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                f"verify (verdict) model returned invalid JSON: {exc}; "
                f"first 200 chars: {text[:200]!r}"
            ) from exc
        if not isinstance(data, dict):
            raise WorkflowError(
                "verify (verdict) model returned non-object JSON - job "
                "failed loud"
            )
        (job_dir / "VERIFIED.json").write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )
        return {"output": "wrote VERIFIED.json",
                "artifact_path": str(job_dir / "VERIFIED.json")}

    return Node(
        id="verdict", kind="prompt", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="Write VERIFIED.json with honest per-claim statuses",
    )


def _verified_json_check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """VERIFIED.json must parse, carry a valid verdict, non-empty claims."""
    p = Path(workdir) / "VERIFIED.json"
    if not p.exists() or p.is_symlink() or not p.is_file():
        return False, ("VERIFIED.json missing (symlinks are never evidence; "
                       "FIFO/device treated as missing - torture-21 F1)")
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - malformed disk JSON degrades to a gate FAIL
        return False, f"VERIFIED.json is not valid JSON: {exc}"
    if not isinstance(d, dict):
        return False, "VERIFIED.json must be a JSON object"
    if d.get("verdict") not in _VERDICTS:
        return False, f"VERIFIED.json verdict must be one of {_VERDICTS}"
    claims = d.get("claims")
    if not isinstance(claims, list) or not claims:
        return False, "VERIFIED.json claims must be a non-empty list"
    for i, c in enumerate(claims):
        if not isinstance(c, dict) or not str(c.get("claim", "")).strip():
            return False, f"VERIFIED.json claim #{i + 1} missing claim text"
        if c.get("status") not in _STATUSES:
            return False, (f"VERIFIED.json claim #{i + 1} status must be "
                           f"one of {_STATUSES}")
        if not str(c.get("evidence", "")).strip():
            return False, f"VERIFIED.json claim #{i + 1} missing evidence"
    return True, (f"VERIFIED.json shape ok ({len(claims)} claims, "
                  f"verdict {d['verdict']})")


_verified_json_check.expected = ["VERIFIED.json"]  # type: ignore[attr-defined]


def _honesty_check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """The cop that audits the cops: VERIFIED.json must not lie.

    1. Every mechanical FAIL in CHECKS.json is reported FAIL.
    2. Claim count matches (nothing dropped, nothing invented).
    3. The report verdict follows the final statuses exactly.
    """
    workdir = Path(workdir)
    try:
        if not (workdir / "CHECKS.json").is_file() or \
                not (workdir / "VERIFIED.json").is_file():
            return False, ("honesty gate: CHECKS.json/VERIFIED.json missing or "
                           "not regular files (FIFO/device treated as missing "
                           "- torture-21 F1)")
        checks = json.loads((workdir / "CHECKS.json").read_text(encoding="utf-8"))
        verified = json.loads((workdir / "VERIFIED.json").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - unreadable disk evidence degrades to a gate FAIL
        return False, f"honesty gate could not read CHECKS.json/VERIFIED.json: {exc}"
    mech = checks.get("claims")
    report = verified.get("claims")
    if not isinstance(mech, list) or not isinstance(report, list):
        return False, "CHECKS.json claims / VERIFIED.json claims must be lists"
    if len(mech) != len(report):
        return False, (f"claim count mismatch: CHECKS.json has {len(mech)}, "
                       f"VERIFIED.json has {len(report)} - the report must "
                       "include EVERY extracted claim exactly once")
    # strict=True is safe: len(mech) == len(report) was asserted just above.
    for i, (m, r) in enumerate(zip(mech, report, strict=True)):
        if m.get("status") == "FAIL" and r.get("status") != "FAIL":
            return False, (f"claim #{i + 1} mechanical FAIL but the report "
                           f"claims {r.get('status')} - a cop cannot hide a "
                           "failed check")
        if m.get("claim") != r.get("claim"):
            return False, (f"claim #{i + 1} text differs between CHECKS.json "
                           "and VERIFIED.json - claims must be copied exactly")
    statuses = [r.get("status") for r in report]
    if "FAIL" in statuses:
        expect = "UNVERIFIED"
    elif "UNCHECKED" in statuses:
        expect = "PARTIAL"
    else:
        expect = "VERIFIED"
    if verified.get("verdict") != expect:
        return False, (f"verdict {verified.get('verdict')!r} does not match "
                       f"the evidence (statuses {statuses}) - expected "
                       f"{expect!r}")
    return True, (f"honesty gate ok: {len(report)} claims, verdict "
                  f"{verified.get('verdict')}")


_honesty_check.expected = ["VERIFIED.json", "CHECKS.json"]  # type: ignore[attr-defined]


def verify_hop() -> Hop:
    """The `verify` lane: inventory -> claims -> mechanical checks -> verdict.

    Gate: every artifact present + VERIFIED.json shape + honesty (mechanical
    FAILs reported, claim count aligned, verdict follows evidence).
    """
    wf = Workflow(id="verify",
                  description="Audit a workspace against a task's claims")
    collect = Node(id="collect", kind="bash", command=_collect_command(),
                   description="Inventory the job's outputs into VERIFY_INVENTORY.md")
    claims = _claims_prompt_node()
    claims.depends_on = ["collect"]
    check = Node(id="check", kind="bash", command=_check_command(),
                 description="Deterministic mechanical checks into CHECKS.json")
    check.depends_on = ["claims"]
    verdict = _verdict_prompt_node()
    verdict.depends_on = ["check"]
    for n in (collect, claims, check, verdict):
        wf.add_node(n)
    return Hop(
        id="verify", workflow=wf,
        required_artifacts=[
            "VERIFY_INVENTORY.md", "CLAIMS.md", "CHECKS.md",
            "CHECKS.json", "VERIFIED.json",
        ],
        gate_checks={
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(
                ["VERIFY_INVENTORY.md", "CLAIMS.md", "CHECKS.md",
                 "CHECKS.json", "VERIFIED.json"]
            ),
            "inventory": file_nonempty_check("VERIFY_INVENTORY.md",
                                              min_chars=20),
            "verified-json": _verified_json_check,
            "honesty": _honesty_check,
        },
        max_fix_loops=2,
    )
