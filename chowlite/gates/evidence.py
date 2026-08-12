"""Evidence gate — the VERIFY step of the chow-lite loop.

An exit code is not success. A job is UNVERIFIED until the evidence gate
produces a verdict from artifacts:

    SHIP  — required evidence present and passing
    FIX   — evidence present but failing; retry with a fix loop
    BLOCK — evidence missing or the job is stuck; needs human/operator

The gate is deliberately deterministic: it reads EVAL.json checks (or a
callable check) and produces a schema-conformant EvidenceVerdict.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GATE_VERSION = "0.1.0"

CheckFn = Callable[[dict[str, Any], Path], tuple[bool, str]]


class EvidenceGate:
    """Runs checks against a job's artifact directory and returns a verdict."""

    def __init__(self, checks: dict[str, CheckFn] | None = None) -> None:
        # check_name -> callable(artifact_ctx, workdir) -> (passed, message)
        self.checks: dict[str, CheckFn] = checks or {}

    def register_check(self, name: str, fn: CheckFn) -> None:
        self.checks[name] = fn

    def evaluate(self, artifact_ctx: dict[str, Any], workdir: Path) -> dict[str, Any]:
        """Evaluate all registered checks. Returns a verdict record.

        artifact_ctx: metadata about produced artifacts (paths, kinds, sizes)
        workdir: where artifacts live on disk
        """
        results: dict[str, Any] = {}
        all_passed = True
        for name, fn in self.checks.items():
            try:
                passed, message = fn(artifact_ctx, workdir)
                results[name] = {"passed": passed, "message": message}
                all_passed = all_passed and passed
            except Exception as exc:  # noqa: BLE001
                results[name] = {"passed": False, "message": f"check error: {exc}"}
                all_passed = False

        if all_passed and results:
            verdict = "SHIP"
            summary = "all evidence checks passed"
        elif results and not all_passed:
            verdict = "FIX"
            summary = "evidence present but checks failed"
        else:
            verdict = "BLOCK"
            summary = "no evidence checks registered — nothing verified"

        return {
            "verdict": verdict,
            "evidence_refs": sorted(artifact_ctx.get("artifact_paths", [])),
            "eval_results": results,
            "summary": summary,
            "verified_at": datetime.now(UTC).isoformat(),
            "gate_version": GATE_VERSION,
        }


def load_eval_json(workdir: Path) -> dict[str, Any] | None:
    """Load EVAL.json if present in the workdir (the standard contract)."""
    p = workdir / "EVAL.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {"error": "EVAL.json is not valid JSON"}
    return None


def eval_json_check(expected_checks: list[str] | None = None) -> CheckFn:
    """Factory: a check that requires EVAL.json with expected checks all pass.

    EVAL.json shape:
        {"checks": [{"name": "...", "passed": true, "message": "..."}, ...]}
    """
    def _check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
        ev = load_eval_json(workdir)
        if ev is None:
            return False, "EVAL.json missing — cannot verify"
        if "error" in ev:
            return False, ev["error"]
        checks = ev.get("checks", [])
        if not checks:
            return False, "EVAL.json has no checks"
        names = [c.get("name") for c in checks]
        if expected_checks and not set(expected_checks).issubset(set(names)):
            return False, f"expected checks {expected_checks} missing from {names}"
        failed = [c for c in checks if not c.get("passed", False)]
        if failed:
            return False, f"{len(failed)} check(s) failed: {[c['name'] for c in failed]}"
        return True, f"{len(checks)} checks passed"

    return _check


def exit_codes_check() -> CheckFn:
    """Factory: a check that all bash nodes exited 0.

    Doctrine: a shell exit code is NOT task success — but a non-zero exit
    code is failing evidence, so the gate must refuse SHIP until it is fixed.
    """
    def _check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
        codes = ctx.get("node_exit_codes", {})
        if not codes:
            return True, "no bash nodes to verify"
        bad = {k: v for k, v in codes.items() if v != 0}
        if bad:
            return False, f"non-zero exit codes: {bad}"
        return True, f"all {len(codes)} bash nodes exited 0"

    return _check


def required_artifact_check(expected: list[str]) -> CheckFn:
    """Factory: a check that requires certain artifact files exist."""
    def _check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
        missing = [e for e in expected if not (workdir / e).exists()]
        if missing:
            return False, f"missing artifacts: {missing}"
        return True, f"artifacts present: {expected}"

    return _check
