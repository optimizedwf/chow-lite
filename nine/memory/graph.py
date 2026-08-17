"""MemoryGraph — durable semantic summaries of hop artifacts.

Contract (mirrors the Cerebras read-path doctrine):
  save_artifact_summary(...)  — write-path: distill then store (never raw)
  search_context(query, k)    — read-path: retrieve minimum viable context

Backends:
  LocalMemoryGraph      — JSONL file (zero-dependency, offline/CI default)
  FirestoreMemoryGraph  — cloud deployment (collection ``nine-memory``);
                          Firestore has no full-text, so search is a
                          recent-window keyword filter — documented as an
                          approximate adapter. Swap in a real metadata graph
                          (e.g. DataHub MCP, hybrid retrieval + RRF) behind
                          the same interface for production-grade search.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


class MemoryGraph(Protocol):
    """The semantic-memory adapter contract."""

    def save_artifact_summary(
        self,
        *,
        job_id: str,
        chain_id: str,
        hop_id: str,
        workflow_id: str,
        artifact_name: str,
        kind: str,
        sha256: str,
        size: int,
        summary: str,
        task_redacted: str,
        verdict: str,
    ) -> str:  # returns the memory document id
        ...

    def search_context(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


class LocalMemoryGraph:
    """Append-only JSONL memory store (same pattern as the route-event log)."""

    def __init__(self, path: str | Path = "jobs/memory.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save_artifact_summary(
        self,
        *,
        job_id: str,
        chain_id: str,
        hop_id: str,
        workflow_id: str,
        artifact_name: str,
        kind: str,
        sha256: str,
        size: int,
        summary: str,
        task_redacted: str,
        verdict: str,
    ) -> str:
        # torture-36 F4: the deterministic id used job_id[:8] + raw artifact
        # basename — distinct jobs sharing an 8-char prefix collided onto
        # ONE document, subdir artifact names embedded '/' into the id,
        # and Firestore (uuid4) disagreed with local. Use a stable hash
        # of the FULL job id + sanitized artifact so (job, artifact) maps
        # 1:1 to a document id, deterministically, with no slash.
        jh = hashlib.sha256(job_id.encode()).hexdigest()[:12]
        art = re.sub(r'[^A-Za-z0-9_.-]', '_', artifact_name.split('.')[0])
        memory_id = f"mem-{jh}-{art}"
        rec = {
            "memory_id": memory_id,
            "job_id": job_id,
            "chain_id": chain_id,
            "hop_id": hop_id,
            "workflow_id": workflow_id,
            "artifact_name": artifact_name,
            "kind": kind,
            "sha256": sha256,
            "size": size,
            "summary": summary,
            "task_redacted": task_redacted,
            "verdict": verdict,
            "created_at": _now(),
        }
        # torture-21 F1 (torture-22 finding 1): memory is a best-effort
        # side effect AFTER the hop verdict is durable — a broken memory
        # store must not fail the chain run (raw traceback) nor 500 the
        # server on an already-shipped job.
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError as exc:
            print(f"WARNING: memory write skipped ({exc}); "
                  "hop verdict already durable", file=sys.stderr)
        return memory_id

    def search_context(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        """Recent-window keyword match over summaries/tasks (approximate)."""
        if not self.path.exists():
            return []
        terms = [t.lower() for t in query.split() if t]
        if not terms:
            return []  # torture-27 F2: an empty/whitespace query must not
            # return the k most-recent records as false "hits" (parity with
            # the Firestore backend which already early-returns []).
        hits: list[dict[str, Any]] = []
        # torture-30 F3: a DIRECTORY (or unreadable path) at the memory
        # store used to raw-crash `nine memory search` with IsADirectoryError
        # — cmd_memory list has the OSError belt, search did not (its open()
        # ran outside any guard, and the cli.py search path only catches
        # bad SHAPE records, not I/O). Same best-effort contract as the
        # save path (torture-21 F1): a broken store degrades to "no
        # matches", never a traceback.
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"WARNING: memory store {self.path} unreadable ({exc}) - "
                  "returning no matches", file=sys.stderr)
            return []
        for line in reversed(text.splitlines()):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue  # one corrupt line must not brick memory search
            if not isinstance(rec, dict):
                continue
            hay = f"{rec.get('summary', '')} {rec.get('task_redacted', '')} {rec.get('artifact_name', '')}".lower()
            if all(t in hay for t in terms):
                hits.append(rec)
                if len(hits) >= k:
                    break
        return hits

    def __len__(self) -> int:
        if not self.path.exists():
            return 0
        return sum(1 for line in open(self.path, encoding="utf-8", errors="replace") if line.strip())


class FirestoreMemoryGraph:
    """Firestore-backed memory (cloud deployment)."""

    def __init__(self, collection: str = "nine-memory") -> None:
        from google.cloud import firestore  # lazy import

        self.db = firestore.Client()
        self.collection = collection

    def _ref(self, memory_id: str):
        return self.db.collection(self.collection).document(memory_id)

    def save_artifact_summary(
        self,
        *,
        job_id: str,
        chain_id: str,
        hop_id: str,
        workflow_id: str,
        artifact_name: str,
        kind: str,
        sha256: str,
        size: int,
        summary: str,
        task_redacted: str,
        verdict: str,
    ) -> str:
        memory_id = f"mem-{uuid.uuid4().hex[:12]}"
        # torture-27 F3 (T22-F1 parity): the cloud backend must be as
        # best-effort as the JSONL one — a Firestore outage/403
        # (google.api_core.exceptions.*, NOT an OSError) mid-hop must not
        # crash the chain nor 500 the server on an already-shipped job.
        try:
            self._ref(memory_id).set({
                "memory_id": memory_id,
                "job_id": job_id,
                "chain_id": chain_id,
                "hop_id": hop_id,
                "workflow_id": workflow_id,
                "artifact_name": artifact_name,
                "kind": kind,
                "sha256": sha256,
                "size": size,
                "summary": summary,
                "task_redacted": task_redacted,
                "verdict": verdict,
                "created_at": _now(),
            })
        except Exception as exc:  # noqa: BLE001 - best-effort side effect
            print(f"WARNING: memory write skipped ({exc}); "
                  "hop verdict already durable", file=sys.stderr)
        return memory_id

    def search_context(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        """Approximate retrieval: recent window (200) + keyword filter.

        Firestore has no full-text search. This is an honest adapter-level
        approximation; production deployments can swap in a metadata graph
        (DataHub MCP / hybrid retrieval + RRF) behind the same interface.
        """
        terms = [t.lower() for t in query.split() if t]
        if not terms:
            return []
        hits: list[dict[str, Any]] = []
        q = self.db.collection(self.collection).order_by(
            "created_at", direction="DESCENDING"
        ).limit(200)
        for doc in q.stream():
            rec = doc.to_dict() or {}
            hay = f"{rec.get('summary','')} {rec.get('task_redacted','')} {rec.get('artifact_name','')}".lower()
            if all(t in hay for t in terms):
                hits.append(rec)
                if len(hits) >= k:
                    break
        return hits


def get_memory_graph(
    path: str | Path = "jobs/memory.jsonl",
    collection: str = "nine-memory",
) -> MemoryGraph | None:
    """Factory: NINE_MEMORY=firestore -> Firestore, NINE_MEMORY=none -> None,
    default/local -> JSONL file. Provider-agnostic knob."""
    mode = os.environ.get("NINE_MEMORY", "local").lower()
    if mode == "none":
        return None
    if mode == "firestore":
        return FirestoreMemoryGraph(collection=collection)
    return LocalMemoryGraph(path=path)
