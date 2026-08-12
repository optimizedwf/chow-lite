"""Smoke-test deploy/demo_probe.py against a real uvicorn server on a random port."""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _wait_health(port: int, timeout: float = 30.0):
    import urllib.request

    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (OSError, ValueError):
            time.sleep(0.2)
    return False


def test_demo_probe_smoke(tmp_path):
    port = 8781
    env = {**os.environ, "GEMINI_API_KEY": "", "PYTHONPATH": os.path.dirname(os.path.dirname(__file__))}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "deploy.server:app", "--port", str(port)],
        cwd=str(tmp_path), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_health(port), "server did not become healthy"
        r = subprocess.run(
            [sys.executable, "deploy/demo_probe.py", f"http://127.0.0.1:{port}"], check=False,
            cwd=os.path.dirname(os.path.dirname(__file__)), capture_output=True, text=True, timeout=90, env=env,
        )
        assert r.returncode == 0, r.stderr[-500:]
        assert "OK" in r.stdout and "/health" in r.stdout and "/v1/stats" in r.stdout
    finally:
        proc.terminate()
        proc.wait(timeout=10)
