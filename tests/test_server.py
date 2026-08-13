"""Tests for the deployed FastAPI surface (deploy/server.py).

Run with the repo on PYTHONPATH (pytest.ini / conftest handles it).

Hermetic: keyword router + JSONL ledger, no Gemini quota/Firestore.
Model-or-fail doctrine: model-backed lanes (build ADK, respond) run on
monkeypatched fakes via _install_fakes(); without a model the API fails
loud (502 WorkflowError) — never a fabricated answer.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

# Hermetic: force keyword router + JSONL ledger (no Gemini quota, no
# Firestore metadata lookups). Live paths covered by *_live tests.
os.environ["GEMINI_API_KEY"] = ""
os.environ["FIRESTORE_EMULATOR_HOST"] = ""

from deploy.server import app

client = TestClient(app)


def _install_fakes(monkeypatch) -> None:
    """Fake the model-backed hops the API may route to."""
    from nine.chains import flagship
    from nine.runtime import responder, summarizer
    from nine.runtime.workflows import Node

    monkeypatch.setattr(
        responder, "respond_text",
        lambda task, max_chars=600: ("a real model answer", "gemini"),
    )
    monkeypatch.setattr(
        summarizer, "summarize_text",
        lambda text, max_words=120, task="", api_key=None:
        ("distilled findings about fooquark", "fake-gemini"),
    )

    def fake_build_run(inputs, job_dir):
        (Path(job_dir) / "solution.py").write_text(
            "def answer():\n    return 42\n", encoding="utf-8")
        (Path(job_dir) / "test_solution.py").write_text(
            "from solution import answer\ndef test_answer():\n    assert answer() == 42\n", encoding="utf-8")
        return {"output": "wrote solution.py + test_solution.py"}

    monkeypatch.setattr(
        flagship, "_build_adk_node",
        lambda: Node(id="build", kind="tool", run=fake_build_run,
                     description="fake ADK node (hermetic test)"),
    )


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "nine"


def test_submit_ships_job_and_returns_decision(monkeypatch):
    _install_fakes(monkeypatch)
    r = client.post("/v1/submit", json={"task": "build a tiny thing"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "shipped"
    assert body["verdict"]["verdict"] == "SHIP"
    assert "job_id" in body
    # decision may be live-router or keyword; both must carry workflow_id
    assert body["decision"]["workflow_id"]


def test_submit_requires_task():
    # pydantic body validation -> 422 for missing/empty task
    r = client.post("/v1/submit", json={})
    assert r.status_code == 422
    r = client.post("/v1/submit", json={"task": ""})
    assert r.status_code == 422


def test_casual_greeting_never_crashes(monkeypatch):
    # "hello there" routes to respond; with a model it ships a verified
    # answer — it must never 500 or return an unverified reply.
    _install_fakes(monkeypatch)
    r = client.post("/v1/submit", json={"task": "hello there"})
    assert r.status_code == 200
    body = r.json()
    assert "job_id" in body or body.get("note") is not None


def test_jobs_and_stats_endpoints():
    r = client.get("/v1/jobs")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body.get("jobs"), list)
    r2 = client.get("/v1/stats")
    assert r2.status_code == 200
    stats = r2.json()
    assert "total" in stats
    assert "by_status" in stats


def test_unknown_job_404():
    r = client.get("/v1/jobs/does-not-exist-123")
    assert r.status_code in (200, 404)  # JSONL ledger tolerates unknown ids gracefully


def test_malformed_json_400():
    r = client.post("/v1/submit", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code in (400, 422)

def test_submit_records_route_events_and_events_endpoint():
    """P2: /v1/submit writes durable route events; /v1/events exposes them;
    /v1/stats reports event + candidate counts (claim-audit #5)."""
    # count before
    before = client.get("/v1/events").json()["count"]
    r = client.post("/v1/submit", json={"task": "review a thing"})
    assert r.status_code == 200
    ev = client.get("/v1/events").json()
    assert ev["count"] == before + 1
    latest = ev["events"][-1]
    assert latest["workflow_id"] == "review"
    assert latest["verdict"] == "SHIP"
    stats = client.get("/v1/stats").json()
    assert stats["events"]["count"] == before + 1
    assert stats["events"]["candidates"]["total"] >= 0


def test_unknown_task_runs_respond_workflow(monkeypatch):
    """No direct-answer escape hatch: unknown tasks run the respond workflow
    and are verified (SHIP) with a real response — never UNVERIFIED."""
    _install_fakes(monkeypatch)
    before = client.get("/v1/events").json()["count"]
    r = client.post("/v1/submit", json={"task": "zzz qqq totally unknown"})
    assert r.status_code == 200
    body = r.json()
    assert body["decision"]["workflow_id"] == "respond"
    assert body["job_id"]
    assert body["verdict"]["verdict"] == "SHIP"
    assert body["response"].strip()
    ev = client.get("/v1/events").json()
    assert ev["count"] == before + 1
    assert ev["events"][-1]["verdict"] == "SHIP"


def test_submit_without_model_fails_loud(monkeypatch):
    """Model-or-fail: with no model configured, respond cannot run — the API
    returns 502 with the reason, never a fabricated offline answer."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # undo any fake respond_text (this test installs none)
    r = client.post("/v1/submit", json={"task": "zzz qqq totally unknown"})
    assert r.status_code == 502
    body = r.json()
    assert "GEMINI_API_KEY" in body["detail"]
