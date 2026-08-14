"""Workflow engine — the EXECUTE step of the nine loop.

A workflow is a DAG of typed nodes:
    prompt  — LLM step (Gemini 3.6 Flash via ADK or raw API)
    bash    — deterministic shell step
    tool    — tool/function call (API, filesystem, etc.)
    subagent— nested agent run (ADK sub-agent)

Nodes produce artifacts that are passed to downstream nodes via the
artifact-passing contract (JSON schema validated). The engine tracks
the job in the ledger and runs the evidence gate at the end.

Design note: this is a *declarative* engine — workflows are data, not code.
"""
from __future__ import annotations

import hashlib
import os
import random
import signal
import subprocess as sp
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from nine.gates.evidence import EvidenceGate
from nine.ledger.ledger import Job, JSONLLedger


class WorkflowError(Exception):
    pass


class NodeTimeoutError(Exception):
    """A callable node exceeded its timeout.

    Deliberately NOT a WorkflowError subclass: _run_node classifies
    WorkflowError as a deterministic failure (retrying cannot fix it) but a
    timeout is transient — a retry may succeed (torture-8 F4: callable
    timeouts were never retried, making max_retries dead code for tool/prompt
    nodes while bash timeouts WERE retried).
    """


@dataclass
class Node:
    """One typed node in a workflow DAG."""
    id: str
    kind: str  # prompt | bash | tool | subagent
    run: Callable[..., Any] | None = None   # callable for tool/prompt nodes
    command: str | None = None              # for bash nodes
    depends_on: list[str] = field(default_factory=list)
    timeout_seconds: int = 300
    max_retries: int = 0                    # transient-failure retries (backoff)
    retry_delay_seconds: float = 1.0        # base delay; doubles per retry
    retry_on_exit: bool = False             # bash: retry on non-zero exit code
    description: str = ""

    def __post_init__(self) -> None:
        # torture-8 F4: timeout_seconds=0 or negative silently made EVERY
        # node fail instantly (bash sp.run(timeout=0) raises TimeoutExpired;
        # callable join(timeout=0) always finds the thread alive). Reject
        # loudly at construction; None = wait forever (documented).
        if self.timeout_seconds is not None and self.timeout_seconds < 1:
            raise ValueError(
                f"node {self.id}: timeout_seconds must be >= 1 or None "
                f"(got {self.timeout_seconds!r}); 0 does NOT mean 'no timeout'"
            )


@dataclass
class Workflow:
    """A named DAG of typed nodes."""
    id: str
    nodes: dict[str, Node] = field(default_factory=dict)
    description: str = ""
    version: str = "0.1.0"

    def add_node(self, node: Node) -> Workflow:
        self.nodes[node.id] = node
        return self

    def topological_order(self) -> list[str]:
        """Kahn's algorithm over depends_on edges."""
        order: list[str] = []
        visited: dict[str, bool] = {nid: False for nid in self.nodes}
        tmp: dict[str, bool] = {}

        def visit(nid: str) -> None:
            if visited[nid]:
                return
            if tmp.get(nid):
                raise WorkflowError(f"cycle detected at node {nid}")
            tmp[nid] = True
            for dep in self.nodes[nid].depends_on:
                if dep in self.nodes:
                    visit(dep)
            tmp[nid] = False
            visited[nid] = True
            order.append(nid)

        for nid in self.nodes:
            visit(nid)
        return order


# torture-14 F4: runtime cache/pytest byproducts are NOT job evidence —
# certifying .pytest_cache/*, __pycache__/*.pyc, or test_output.log as
# shipped artifacts would pollute `nine artifacts`, memory lineage, and
# evidence replay. .nine-node-pids is the runtime's own process-group
# tracker (torture-13 F2) — also never evidence.
_MANIFEST_IGNORE_DIRS = (".pytest_cache", "__pycache__", ".git")
_MANIFEST_IGNORE_SUFFIXES = (".pyc", ".log")
_MANIFEST_IGNORE_NAMES = (".nine-node-pids",)


def _is_ignored(rel: str, p: Path) -> bool:
    """One byproduct-exclusion predicate for BOTH inventory paths.

    torture-14 F4 + torture-15 F3/F4: pytest cache/pyc/log byproducts and
    the runtime's own .nine-node-pids tracker are never job evidence. The
    recursive inventory AND the explicit artifact_path branch must agree —
    a tool naming test_output.log or .nine-node-pids as artifact_path must
    not re-certify what F4 removed. Name matching covers ANY path part
    (nested sub/.nine-node-pids); suffix matching is case-insensitive
    (output.LOG).
    """
    parts = rel.split("/")
    if any(part in _MANIFEST_IGNORE_DIRS for part in parts):
        return True
    if p.suffix.lower() in _MANIFEST_IGNORE_SUFFIXES:
        return True
    if any(part in _MANIFEST_IGNORE_NAMES for part in parts):
        return True
    return False


def _manifest_files(job_dir: Path) -> list[tuple[str, Path]]:
    """Recursive file inventory: (relative name, path), symlinks + dirs skipped.

    torture-11 F5: the manifest must track SUBDIRECTORY files too — gates
    certify reviews/security.md (review-multi) and solution/*.py
    (build-multi); top-level-only tracking let stale attempt-1 subdir
    content certify a SHIP. Relative names disambiguate same-named files.
    rglob does not descend into symlinked dirs; symlinked files are skipped
    here anyway (never evidence). torture-14 F4: pytest cache/pyc/log
    byproducts and the runtime's own .nine-node-pids tracker are excluded
    (torture-15 F4: via the shared predicate, so nested + case variants too).
    """
    out: list[tuple[str, Path]] = []
    for p in sorted(job_dir.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        rel = p.relative_to(job_dir).as_posix()
        if _is_ignored(rel, p):
            continue
        out.append((rel, p))
    return out


def _record_node_pid(job_dir: Path, pid: int) -> None:
    """Best-effort append of a bash-node process-group leader pid + spawn time.

    The pid file (.nine-node-pids, excluded from manifests) lets an external
    supervisor (bench_nine timeout cleanup) kill detached node groups after
    the CLI itself is dead (torture-13 F2). torture-15 F9: the spawn
    wall-clock time is recorded alongside so the external killer can verify
    the pid still refers to the SAME process (a recycled pid must never be
    SIGKILLed). Purely a cleanup aid — failure to write it never affects
    job correctness.
    """
    try:
        with open(job_dir / ".nine-node-pids", "a", encoding="utf-8") as fh:
            fh.write(f"{pid} {int(time.time())}\n")
    except OSError:
        pass


def _prune_node_pid(job_dir: Path, pid: int) -> None:
    """Best-effort removal of a node's pid line once the node completed.

    torture-15 F9: without pruning, every bash node leaves a stale pid in
    the file for the rest of the run — when the OS reuses that pid within
    the run window, the external killer would SIGTERM/SIGKILL an innocent
    process group. The runtime knows when a node is done; remove it.
    """
    try:
        f = job_dir / ".nine-node-pids"
        if not f.exists():
            return
        lines = f.read_text(encoding="utf-8").splitlines()
        kept = [ln for ln in lines
                if not (ln.strip().split()[0] == str(pid)
                        if ln.strip() else False)]
        f.write_text("\n".join(kept) + ("\n" if kept else ""),
                     encoding="utf-8")
    except OSError:
        pass


class WorkflowExecutor:
    """Executes a workflow DAG, producing artifacts + a verdict.

    Args:
        ledger: JSONLLedger for job tracking
        gate: EvidenceGate for the final verdict
        workdir: directory where artifacts land
    """

    def __init__(
        self,
        ledger: JSONLLedger,
        gate: EvidenceGate,
        workdir: str | Path = "work",
        job_dir_override: str | Path | None = None,
    ) -> None:
        self.ledger = ledger
        self.gate = gate
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        # chains run every hop in ONE shared directory so artifacts hand off
        self.job_dir_override = Path(job_dir_override) if job_dir_override else None
        # torture-12 F1: the run-input snapshot must survive across
        # execute() calls. The chain path calls execute() once PER HOP
        # ATTEMPT on the same executor; a local variable would re-snapshot
        # after attempt 1 wrote files, turning attempt-1 evidence into
        # 'run inputs' for attempt 2 (stale SHIP). Key by job_dir so the
        # same executor never confuses two jobs. Value = rel name -> sha256
        # of the attempt-1 content (torture-13 F3: the run-input exemption
        # only holds while the file is UNCHANGED — a run that modifies an
        # input turns it into run-produced evidence that must be
        # re-registered in the shipping attempt).
        self._first_attempt_before: dict[str, dict[str, str]] = {}

    def _hash(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _cancelled(self, job: Job) -> bool:
        """Fresh ledger read: did an operator cancel this job?

        A cancel from another process/thread appends a `cancelled` line to
        the ledger file that this process's in-memory copy never sees
        (last-line-wins at load time). Poll the DURABLE status so a running
        job stops instead of stamping `shipped` over the operator's cancel
        (torture-8 F3).
        """
        try:
            live = self.ledger.refresh(job.job_id)
        except Exception:  # noqa: BLE001 - best-effort poll
            return False
        return live.status == "cancelled"

    def _abort_cancelled(
        self, job: Job, artifacts: list[dict[str, Any]],
        attempt: int, node_outputs: dict[str, Any], node_meta: dict[str, Any],
    ) -> dict[str, Any]:
        """Terminal abort when the job was cancelled mid-run.

        The durable ledger already says `cancelled` (operator's line) — do
        NOT append a shipped/blocked line over it. Return a CANCELLED
        verdict so the caller reports the truth.
        """
        from datetime import UTC, datetime

        job.status = "cancelled"  # direct terminal set; matches durable truth
        verdict: dict[str, Any] = {
            "verdict": "CANCELLED",
            "evidence_refs": sorted(a["path"] for a in artifacts),
            "eval_results": {},
            "summary": "cancelled by operator during execution",
            "verified_at": datetime.now(UTC).isoformat(),
            "gate_version": None,
        }
        job.add_verdict(verdict)
        job.metadata["nodes"] = node_meta
        job.metadata["attempts"] = attempt
        return {
            "job_id": job.job_id,
            "verdict": verdict,
            "artifacts": artifacts,
            "attempts": attempt,
            "node_outputs": dict(node_outputs),
            "node_meta": dict(node_meta),
        }

    def _run_node_once(self, node: Node, inputs: dict[str, Any], job_dir: Path) -> dict[str, Any]:
        """Execute one node ONCE, returning {output, artifact?, ...}."""
        if node.kind == "bash":
            if not node.command:
                raise WorkflowError(f"node {node.id}: bash node needs command")
            # bash nodes resolve `python` to the SAME interpreter running
            # nine (venv), so scripts can rely on project deps (pandas,
            # matplotlib) without hard-coding an absolute interpreter path.
            bash_env = dict(os.environ)
            pybin = str(Path(sys.executable).parent)
            bash_env["PATH"] = pybin + os.pathsep + bash_env.get("PATH", "")
            proc = sp.Popen(
                node.command, shell=True, cwd=job_dir,
                stdout=sp.PIPE, stderr=sp.PIPE, text=True,
                env=bash_env, start_new_session=True,
            )
            # torture-13 F2: the node runs in its OWN session/process group
            # (start_new_session). When an EXTERNAL killer (bench per-fixture
            # timeout) kills the nine CLI, the CLI's own timeout handler —
            # which would killpg the node group — dies with it, and the
            # detached node tree keeps writing into the abandoned job dir.
            # Record the group leader so the external killer can clean up.
            _record_node_pid(job_dir, proc.pid)
            try:
                out, err = proc.communicate(timeout=node.timeout_seconds)
            except sp.TimeoutExpired:
                # torture-8 F5: on timeout the shell is SIGKILLed but
                # grandchildren (nohup server &, test daemons) survive and
                # drop ghost files after the job failed. start_new_session
                # puts the shell in its OWN process group; SIGTERM the whole
                # group, grace period, then SIGKILL so nothing outlives the
                # failed attempt.
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):  # noqa: BLE001
                    pass
                try:
                    time.sleep(min(2.0, node.retry_delay_seconds))
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):  # noqa: BLE001
                    pass
                out, err = proc.communicate()
                _prune_node_pid(job_dir, proc.pid)
                raise
            # torture-15 F9: the node completed — its pid must not linger
            # for the external killer to mis-attribute to a recycled process.
            _prune_node_pid(job_dir, proc.pid)
            return {"exit_code": proc.returncode, "stdout": (out or "")[-2000:],
                    "stderr": (err or "")[-2000:]}
        if node.kind in ("prompt", "tool", "subagent", "summarize"):
            if node.run is None:
                raise WorkflowError(f"node {node.id}: {node.kind} node needs a callable")
            # torture T3-F5: timeout_seconds was enforced ONLY for bash; a
            # hung model/tool call (free-tier stall) left the job running
            # forever. Enforce the same deadline for callable nodes — the
            # worker thread is abandoned (daemon) but the JOB fails loud.
            run = node.run  # narrowed local (mypy can't narrow attrs in closures)
            deadline = node.timeout_seconds
            result: dict[str, Any] = {}
            error: BaseException | None = None

            def _call() -> None:
                nonlocal result, error
                try:
                    out = run(inputs, job_dir)
                    result = out if isinstance(out, dict) else {"output": out}
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    error = exc

            worker = threading.Thread(target=_call, daemon=True)
            worker.start()
            worker.join(timeout=deadline)
            if worker.is_alive():
                # torture-6 F5 (partial): a timed-out daemon thread is
                # ABANDONED and may still write files after this attempt.
                # Python cannot kill threads, so the executor records the
                # fact in job metadata at the call site (see execute()).
                # torture-8 F4: raise the RETRYABLE NodeTimeoutError — the
                # old WorkflowError was classified deterministic and callable
                # timeouts were never retried (max_retries was dead code).
                raise NodeTimeoutError(
                    f"node {node.id} exceeded timeout {deadline}s"
                )
            if error is not None:
                raise error
            return result
        raise WorkflowError(f"node {node.id}: unknown kind {node.kind}")

    def _run_node(self, node: Node, inputs: dict[str, Any], job_dir: Path) -> tuple[dict[str, Any], int]:
        """Run a node with retry/backoff for transient failures.

        Retries on any raised exception (timeout, Gemini 429/503, flaky
        tool) and — for bash nodes with retry_on_exit — on non-zero exit
        codes. Backoff = retry_delay_seconds * 2**attempt (+/-10% jitter).
        Returns (result, attempts_used).
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                result = self._run_node_once(node, inputs, job_dir)
                if (
                    node.kind == "bash"
                    and node.retry_on_exit
                    and result.get("exit_code", 0) != 0
                    and attempts <= node.max_retries
                ):
                    time.sleep(self._backoff(node, attempts))
                    continue
                return result, attempts
            except WorkflowError:
                # Deterministic failure (no key, bad input, missing callable) —
                # retrying cannot fix it. Fail loud immediately.
                raise
            except Exception as exc:  # noqa: BLE001 — transient failures retried
                if attempts > node.max_retries:
                    raise WorkflowError(f"node {node.id} failed after {attempts} attempts: {exc}") from exc
                time.sleep(self._backoff(node, attempts))

    @staticmethod
    def _backoff(node: Node, attempt: int) -> float:
        base = node.retry_delay_seconds * (2 ** (attempt - 1))
        return base * (1 + random.uniform(-0.1, 0.1))

    def execute(
        self,
        workflow: Workflow,
        job: Job,
        inputs: dict[str, Any],
        fix_loop: bool = True,
    ) -> dict[str, Any]:
        """Run the workflow for a job, with an in-engine FIX loop.

        A FIX verdict re-runs the workflow (up to job.max_fix_loops) with a
        `fix_directive` describing exactly which gate checks failed — the
        single-workflow path (`nine submit`, POST /v1/submit) self-heals
        instead of leaving jobs stuck at `fixing`. Chains pass fix_loop=False
        (they re-run whole hops at the hop level).

        Returns the final verdict + artifacts + per-node timing/attempts.
        """
        inputs = dict(inputs)
        # lifecycle: submitted -> routing (once per job)
        if job.status == "submitted":
            job.transition("routing")
            self.ledger.update(job)

        job_dir = self.job_dir_override or (self.workdir / job.job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        order = workflow.topological_order()

        node_outputs: dict[str, Any] = {}
        node_meta: dict[str, dict[str, Any]] = {}
        verdict: dict[str, Any] = {"verdict": "BLOCK"}
        attempt = 0
        # torture-10 F2: files present at the START of attempt 1 are RUN
        # INPUTS (task.txt, chain handoffs, bench-seeded test files, a
        # seeded solution.py) — the run never "produces" them and the
        # per-attempt manifest legitimately never registers them. Only
        # run-PRODUCED files (absent from this first snapshot) must appear
        # in the SHIPping attempt's registered manifest. The snapshot
        # itself lives on the executor (self._first_attempt_before, keyed
        # by job_dir) so chain hop attempts share it (torture-12 F1).

        while True:
            attempt += 1
            job.attempts += 1
            job.transition("running")
            if self._cancelled(job):
                return self._abort_cancelled(
                    job, [], attempt, node_outputs, node_meta)
            job.artifacts = []  # manifest = this attempt's artifacts only
            # manifest snapshot: files present + untouched BEFORE this attempt
            # are NOT this attempt's artifacts. In chains every hop runs a
            # fresh executor over the SAME job_dir — without the snapshot a
            # later hop would re-register earlier hops' files as its own
            # (torture findings T1-F5/T2-F4: duplicate + misattributed
            # manifest entries). FIX reruns likewise only re-register files
            # this attempt actually rewrote.
            before_files = _manifest_files(job_dir)
            before: dict[str, tuple[int, int]] = {}
            for rel, p in before_files:
                # torture-8 F1: symlinks are NEVER evidence - a symlink to an
                # outside file must not even enter the snapshot (the helper
                # skips them).
                st = p.stat()
                before[rel] = (st.st_size, st.st_mtime_ns)
            key = str(job_dir)
            if key not in self._first_attempt_before:
                # attempt-1 snapshot = run inputs for the WHOLE job (torture-12
                # F1: persisted per job_dir across execute() calls so chain
                # hop attempts share it). torture-13 F3: record the CONTENT
                # digest too — the exemption for a run input only holds while
                # the file is unchanged; a run that modifies an input makes it
                # run-produced evidence (must be registered in the shipping
                # attempt's manifest).
                snap = {}
                for rel, p in before_files:
                    # torture-15 F2: an unreadable input (chmod 000,
                    # permission drift) must not raw-crash execute() — skip
                    # it from the snapshot so the stale guard treats it as
                    # run-produced (BLOCK) instead of exempting it.
                    try:
                        snap[rel] = self._hash(p.read_bytes())
                    except OSError:
                        continue
                self._first_attempt_before[key] = snap

            seen: dict[str, str] = {}  # reset per attempt: reruns re-register the full dir
            seen_idx: dict[str, int] = {}  # rel -> position in artifacts (same-name replace)
            self.ledger.update(job)

            artifacts: list[dict[str, Any]] = []
            node_exit_codes: dict[str, int] = {}

            for nid in order:
                node = workflow.nodes[nid]
                node_inputs = {
                    "task": inputs.get("task", ""),
                    "node": nid,
                    "job_id": job.job_id,
                    "attempt": attempt,
                    "fix_directive": inputs.get("fix_directive", ""),
                }
                # torture-7 F8: the documented artifact-passing contract
                # (chain_inputs['hop_artifacts'] -> node inputs) was dead
                # code - the chain set it but the executor never forwarded
                # it. Pass it through so plugin hops written against the
                # docs actually see the previous hop's artifact paths.
                if "hop_artifacts" in inputs:
                    node_inputs["hop_artifacts"] = inputs["hop_artifacts"]
                for dep in node.depends_on:
                    if dep in node_outputs:
                        node_inputs[dep] = node_outputs[dep]
                started = monotonic()
                try:
                    result, attempts_used = self._run_node(node, node_inputs, job_dir)
                except Exception as exc:
                    # torture-6 F5 (partial): a timed-out callable node leaves
                    # an abandoned daemon thread that may still write files.
                    # Record it in the job (operators see it; recover wipes
                    # the job dir before re-execution, clearing ghost files).
                    if isinstance(exc, (WorkflowError, NodeTimeoutError)) and "exceeded timeout" in str(exc):
                        try:
                            job.metadata["timeout_abandoned_worker"] = {
                                "node": node.id,
                                "deadline_s": getattr(node, "timeout_seconds", None),
                            }
                            self.ledger.update(job)
                        except Exception:  # noqa: BLE001 - best-effort note
                            pass
                    job.transition("failed")
                    self.ledger.update(job)
                    raise WorkflowError(f"node {nid} failed: {exc}") from exc
                duration_ms = round((monotonic() - started) * 1000)
                node_meta[nid] = {
                    "attempts": attempts_used,
                    "duration_ms": duration_ms,
                    "node_attempt": attempt,
                }
                node_outputs[nid] = result
                if node.kind == "bash" and "exit_code" in result:
                    node_exit_codes[nid] = result["exit_code"]

                # artifact registration: name+content-deduped across attempts
                # (a FIX rerun that rewrites a file refreshes its sha256).
                # Recursive (torture-11 F5): subdir files (reviews/*.md,
                # solution/*.py) are registered with RELATIVE names so gates
                # certifying them can prove per-attempt provenance.
                # torture-14 F4: two nodes writing the same name within one
                # attempt (flagship build node + self-test node both write
                # EVAL.json) used to produce two conflicting entries — the
                # last writer is the final disk state the gate certifies, so
                # a same-name different-hash registration REPLACES the
                # earlier entry instead of appending a duplicate.
                def _register(name: str, path: str, kind: str, h: str,
                              size: int, produced_by: str,
                              seen=seen, seen_idx=seen_idx,
                              artifacts=artifacts) -> None:
                    # B023-safe: the per-attempt dicts are bound as defaults
                    # (the closure is redefined per node; the objects are the
                    # per-attempt mutable state, mutated in place).
                    artifact = {
                        "name": name,
                        "path": path,
                        "kind": kind,
                        "sha256": h,
                        "size": size,
                        "produced_by": produced_by,
                        "produced_at": job.updated_at,
                    }
                    if seen.get(name) == h:
                        return  # same content already registered
                    if name in seen_idx:
                        idx = seen_idx[name]
                        artifacts[idx] = artifact
                        job.artifacts[idx] = artifact
                        seen[name] = h
                        return
                    seen[name] = h
                    seen_idx[name] = len(artifacts)
                    artifacts.append(artifact)
                    job.add_artifact(artifact)

                for rel, p in _manifest_files(job_dir):
                    # torture-8 F1: is_file()/stat()/read_bytes() follow
                    # symlinks, so a symlink to a REAL outside file would be
                    # registered with the outside sha256/size as this job's
                    # evidence. Symlinks are never evidence - skip them.
                    st = p.stat()
                    if before.get(rel) == (st.st_size, st.st_mtime_ns):
                        continue  # pre-existing, untouched this attempt
                    data = p.read_bytes()
                    h = self._hash(data)
                    kind = "document"
                    if p.suffix in (".py", ".js", ".ts", ".go", ".sh", ".json", ".yaml", ".yml"):
                        kind = "code"
                    elif p.suffix in (".png", ".jpg", ".mp4", ".wav"):
                        kind = "media"
                    elif p.suffix in (".csv", ".jsonl", ".parquet", ".db", ".sqlite"):
                        kind = "data"
                    _register(rel, str(p), kind, h, len(data), nid)

                # explicit artifact paths in node output (tool nodes)
                for key in ("artifact", "artifact_path"):
                    val = result.get(key)
                    if val:
                        p = Path(val) if isinstance(val, str) else val
                        # torture-8 F1: an explicitly-registered artifact that
                        # is a symlink certifies OUTSIDE content - skip it.
                        if p.is_symlink():
                            continue
                        if p.exists():
                            # relative name when the tool certifies a file
                            # inside the job dir (subdir artifacts included);
                            # dedupe on the RELATIVE key, not the basename
                            # (torture-13 F4: a subdir artifact_path was
                            # registered twice because seen was keyed by
                            # p.name while the manifest is relative).
                            try:
                                rel = p.relative_to(job_dir).as_posix()
                            except ValueError:
                                # torture-15 F5: an artifact OUTSIDE the job
                                # dir must not register under its bare
                                # basename — that collides with (and
                                # silently REPLACES) a same-named inside file
                                # and makes the manifest point outside the
                                # job dir. Namespace external rels so they can
                                # never collide with an inside rel.
                                rel = "../" + p.name
                            # torture-15 F3: the explicit branch must apply
                            # the SAME byproduct exclusion as the recursive
                            # inventory — a tool naming test_output.log /
                            # .nine-node-pids / __pycache__ as artifact_path
                            # must not re-certify them as shipped evidence.
                            if _is_ignored(rel, p):
                                continue
                            data = p.read_bytes()
                            h = self._hash(data)
                            _register(rel, str(p), "other", h, len(data), nid)

            if self._cancelled(job):
                return self._abort_cancelled(
                    job, artifacts, attempt, node_outputs, node_meta)
            job.transition("awaiting_evidence")
            self.ledger.update(job)

            artifact_ctx = {
                "artifact_paths": [a["path"] for a in artifacts],
                "artifacts": artifacts,
                "node_exit_codes": node_exit_codes,
            }
            verdict = self.gate.evaluate(artifact_ctx, job_dir)
            # torture-7 F1 + torture-10 F2: the gate reads DISK while the
            # manifest is a per-attempt snapshot. On a FIX re-run the same
            # job_dir keeps attempt-1 files, so a gate that passes on a
            # stale file (not rewritten this attempt) would SHIP artifacts
            # whose certifying evidence is NOT in the shipped manifest. A
            # SHIP must have produced its evidence THIS attempt: for EVERY
            # gate check's certified files (tagged `.expected` on the check
            # fn), a file that exists on disk but was not registered in this
            # attempt's manifest is stale - downgrade to BLOCK. Covers
            # EVAL.json (eval_json_check), required_artifact_check lists,
            # file_nonempty_check files, subdir files (torture-11 F5:
            # reviews/*.md now tracked by the recursive manifest), and
            # directory artifacts (solution/ - certified by content).
            if verdict["verdict"] == "SHIP":
                registered = {a["name"] for a in artifacts}
                snap = self._first_attempt_before.get(str(job_dir)) or {}
                inputs_ok = set(snap)
                stale: list[str] = []
                for _name, fn in self.gate.checks.items():
                    for expected_name in (getattr(fn, "expected", None) or []):
                        p_expected = job_dir / expected_name
                        if not p_expected.exists():
                            continue  # the check already failed
                        if p_expected.is_symlink():
                            # torture-15 F1: a symlink at an expected input
                            # path is a CONTENT CHANGE, not "not evidence" —
                            # the exemption is for unchanged run inputs, and a
                            # symlink can never match the attempt-1 content
                            # digest. Skipping let a symlink-following check
                            # certify outside content with the file ABSENT
                            # from the shipped manifest (SHIP). Treat the
                            # substitution as stale: BLOCK.
                            stale.append(expected_name)
                            continue
                        if p_expected.is_dir():
                            # directory artifact (build-multi solution/):
                            # certified by its CONTENT - stale unless a file
                            # under it was produced this attempt (torture-11
                            # F5), unless it was a run input from attempt 1
                            # (torture-13 F3: only while UNCHANGED since the
                            # attempt-1 snapshot - a run that modifies an
                            # input dir makes it run-produced evidence).
                            prefix = expected_name.rstrip("/") + "/"
                            produced_under = any(
                                r.startswith(prefix) for r in registered)
                            if produced_under:
                                continue
                            input_under = [r for r in inputs_ok
                                           if r.startswith(prefix)]
                            if input_under:
                                def _member_modified(
                                    r: str, snap: dict = snap
                                ) -> bool:
                                    # B023: snap bound as a DEFAULT ARG so
                                    # the closure sees THIS iteration's
                                    # snapshot (defaults capture the current
                                    # object; the closure is redefined per
                                    # loop iteration anyway).
                                    mp = job_dir / r
                                    if not mp.is_file() or mp.is_symlink():
                                        return True  # gone/replaced
                                    try:
                                        return self._hash(
                                            mp.read_bytes()
                                        ) != snap.get(r)
                                    except OSError:
                                        # torture-15 F2: an unreadable member
                                        # is not unchanged content.
                                        return True
                                modified = any(_member_modified(r)
                                               for r in input_under)
                                if not modified:
                                    continue
                            stale.append(expected_name.rstrip("/") + "/")
                            continue
                        if expected_name in registered:
                            continue  # produced this attempt
                        if expected_name in inputs_ok:
                            # run input/seeded (task.txt, HANDOFF.md,
                            # test_solution.py, seeded solution.py): the run
                            # never needs to re-produce its own inputs —
                            # EXCEPT once the run MODIFIES one (torture-13
                            # F3): a gate-certified input rewritten in an
                            # earlier attempt is run-produced evidence that
                            # must be re-registered in the shipping attempt,
                            # or the manifest omits the certified content.
                            try:
                                unchanged = self._hash(
                                    p_expected.read_bytes()
                                ) == snap.get(expected_name)
                            except OSError:
                                # torture-15 F2: an unreadable input is not
                                # unchanged content — BLOCK, never raise.
                                unchanged = False
                            if unchanged:
                                continue
                            stale.append(expected_name)
                            continue
                        # run-PRODUCED file that exists on disk but was NOT
                        # registered this attempt -> the gate certified
                        # stale evidence from an earlier attempt (torture-7
                        # F1 for EVAL.json; torture-10 F2 for every other
                        # gate-certified file; torture-11 F5 for subdir
                        # files now that the manifest is recursive).
                        stale.append(expected_name)
                if stale:
                    verdict = {
                        "verdict": "BLOCK",
                        "evidence_refs": sorted(artifact_ctx.get("artifact_paths", [])),
                        "eval_results": dict(verdict.get("eval_results", {})),
                        "summary": (
                            "stale artifact(s): "
                            f"{sorted(set(stale))} - the gate passed on "
                            "file(s) not produced this attempt - certifying "
                            "evidence missing from the shipped manifest "
                            "(torture-7 F1 / torture-10 F2 / torture-11 F5)"),
                        "verified_at": (verdict.get("verified_at")
                                        or datetime.now(UTC).isoformat()),
                        "gate_version": verdict.get("gate_version", ""),
                    }

            job.add_verdict(verdict)

            if verdict["verdict"] == "SHIP":
                job.transition("shipped")
                self.ledger.update(job)
                break

            if verdict["verdict"] == "FIX" and fix_loop and job.attempts <= job.max_fix_loops:
                job.transition("fixing")
                self.ledger.update(job)
                failures = [
                    f"{k}: {v['message']}"
                    for k, v in verdict["eval_results"].items()
                    if not v.get("passed")
                ]
                inputs["fix_directive"] = (
                    f"gate FIX after attempt {attempt}: "
                    + ("; ".join(failures) if failures else verdict.get("summary", ""))
                    + ". Rework the artifacts and re-run."
                )
                continue

            job.transition("blocked")
            self.ledger.update(job)
            break

        job.metadata["nodes"] = node_meta
        job.metadata["attempts"] = attempt
        self.ledger.update(job)
        return {
            "job_id": job.job_id,
            "verdict": verdict,
            "artifacts": artifacts,
            "attempts": attempt,
            "node_outputs": {k: v for k, v in node_outputs.items()},
            "node_meta": dict(node_meta),
        }


