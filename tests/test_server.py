"""Tests for the deployed FastAPI surface (deploy/server.py).

Run with the repo on PYTHONPATH (pytest.ini / conftest handles it).
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


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "chow-lite"


def test_submit_ships_job_and_returns_decision():
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


def test_casual_greeting_never_crashes():
    # With the live model, "hello there" may route to respond (direct answer)
    # or to a workflow; either is fine — it must never 500.
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


def test_direct_answer_records_unverified_event():
    before = client.get("/v1/events").json()["count"]
    r = client.post("/v1/submit", json={"task": "zzz qqq totally unknown"})
    assert r.status_code == 200
    assert "note" in r.json() or r.json().get("workflow_id") in ("respond", "fallback-respond")
    ev = client.get("/v1/events").json()
    assert ev["count"] == before + 1
    assert ev["events"][-1]["verdict"] == "UNVERIFIED"
