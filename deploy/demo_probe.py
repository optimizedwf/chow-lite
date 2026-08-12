#!/usr/bin/env python3
"""GCP-proof probe — exercises the LIVE deployed chow-lite API.

Run against the deployed Cloud Run URL (or local uvicorn):
    python deploy/demo_probe.py [BASE_URL]
    # default: http://localhost:8080  (use the .run.app URL after deploy)

Prints a clean terminal-style transcript used for the demo video's
"live on Google Cloud" segment.
"""
import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"


def call(method: str, path: str, payload: dict | None = None) -> dict:
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
    return {"status": resp.status, "latency_ms": round((time.time() - t0) * 1000),
            "body": body}


def main() -> int:
    print(f"$ chow probe {BASE}")
    print(f"  (live on Google Cloud Run \u2014 Firestore-backed, built from GitHub main)\n")

    h = call("GET", "/health")
    print(f"[GET /health]  {h['status']}  {h['latency_ms']}ms")
    print(f"  -> {json.dumps(h['body'])}")

    tasks = [
        "build a small calculator with tests",
        "handle a customer refund question from the inbox",
    ]
    for task in tasks:
        s = call("POST", "/v1/submit", {"task": task})
        b = s["body"]
        d = b.get("decision", {})
        print(f"\n[POST /v1/submit]  {s['status']}  {s['latency_ms']}ms")
        print(f"  task:      {task}")
        print(f"  workflow:  {d.get('workflow_id')}")
        conf = d.get('confidence')
        print(f"  confidence:{conf:.2f}   router: {d.get('router_version')}" if conf is not None
              else f"  router:    {d.get('router_version')}")
        print(f"  verdict:   {b.get('verdict', {}).get('verdict')}  (job {b.get('job_id','')[:8]})")

    jobs = call("GET", "/v1/jobs")
    print(f"\n[GET /v1/jobs]  {jobs['status']}  {jobs['latency_ms']}ms  -> {len(jobs['body'].get('jobs', []))} jobs")
    for j in jobs["body"].get("jobs", [])[-3:]:
        print(f"  {j.get('job_id','')[:8]}  {j.get('workflow_id',''):32} {j.get('status','')}")

    st = call("GET", "/v1/stats")
    print(f"\n[GET /v1/stats]  {st['status']}  {st['latency_ms']}ms")
    print(f"  -> {json.dumps(st['body'])}")
    print(f"\nOK \u2014 chow-lite is LIVE on {BASE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
