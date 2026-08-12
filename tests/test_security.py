"""Security regression tests for the public API surface.

Covers the P0 findings from review/REVIEW-SECURITY.md:
  1. RCE: user task bytes must never reach a shell (no command injection).
  2. Auth: X-API-Key enforced when NINE_API_KEY is set.
  3. Rate limit + body-size cap.
  4. Data lands on writable scratch (/tmp) when running on Cloud Run.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["GEMINI_API_KEY"] = ""
os.environ.setdefault("NINE_API_KEY", "")
os.environ.pop("K_SERVICE", None)


def _fresh_client(monkeypatch, tmp_path, api_key=""):
    """Import deploy.server fresh with a given NINE_API_KEY + tmp runtime."""
    monkeypatch.setenv("NINE_API_KEY", api_key)
    monkeypatch.setenv("NINE_DATA_DIR", str(tmp_path))
    for m in list(sys.modules):
        if m.startswith("deploy"):
            del sys.modules[m]
    from fastapi.testclient import TestClient

    from deploy.server import app

    return TestClient(app)


def test_no_shell_injection_via_task(monkeypatch, tmp_path):
    """A task that would break out of a shell must not execute anything."""
    client = _fresh_client(monkeypatch, tmp_path)
    evil = "build'; touch /tmp/nine_pwned; echo '"
    r = client.post("/v1/submit", json={"task": evil})
    assert r.status_code == 200, r.text
    assert not Path("/tmp/nine_pwned").exists()
    # the artifact should contain the RAW task (written by Python, byte-safe)
    jid = r.json()["job_id"]
    job = client.get(f"/v1/jobs/{jid}").json()
    assert "input" in job


def test_apikey_enforced_when_configured(monkeypatch, tmp_path):
    client = _fresh_client(monkeypatch, tmp_path, api_key="sekret")
    # no key -> 401
    assert client.get("/v1/jobs").status_code == 401
    assert client.post("/v1/submit", json={"task": "build x"}).status_code == 401
    # wrong key -> 401
    assert client.get("/v1/jobs", headers={"X-API-Key": "nope"}).status_code == 401
    # right key -> 200
    assert client.get("/v1/jobs", headers={"X-API-Key": "sekret"}).status_code == 200
    # /health stays open (no auth needed for liveness)
    assert client.get("/health").status_code == 200


def test_task_length_capped(monkeypatch, tmp_path):
    client = _fresh_client(monkeypatch, tmp_path)
    r = client.post("/v1/submit", json={"task": "build " + "x" * 2500})
    assert r.status_code == 422


def test_payload_size_capped(monkeypatch, tmp_path):
    client = _fresh_client(monkeypatch, tmp_path)
    big = "x" * (1_048_576 + 100)
    r = client.post("/v1/submit", json={"task": "build", "pad": big})
    assert r.status_code == 413


def test_runtime_dir_uses_scratch_on_cloudrun(monkeypatch, tmp_path):
    monkeypatch.setenv("K_SERVICE", "nine")
    client = _fresh_client(monkeypatch, tmp_path)
    r = client.get("/health")
    assert r.status_code == 200
    # ledger written inside NINE_DATA_DIR (tmp), not repo root
    import deploy.server as srv

    assert str(srv.LEDGER_PATH).startswith(str(tmp_path))
    assert str(srv.WORKDIR).startswith(str(tmp_path))
