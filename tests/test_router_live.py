"""Live GeminiRouter test — requires GEMINI_API_KEY (skips otherwise)."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY not set",
)


def _router():
    from google import genai

    from nine.router.classifier import Router

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    class Model:
        def generate_content(self, prompt):
            return client.models.generate_content(
                model="gemini-3.6-flash", contents=prompt
            )

    r = Router(model=Model(), version="live-test")
    r.register("research", ["research", "investigate"],
               "Produce a findings document (research.md).")
    r.register("build", ["build", "implement", "write code"],
               "Implement from a plan; produce code + EVAL.json.")
    r.register("review", ["review", "audit", "qa"],
               "Review a build; produce review.md verdict.")
    return r


def test_live_router_classifies_tasks():
    r = _router()
    d = r.classify("please research the history of the printing press")
    assert d.workflow_id == "research"
    assert d.confidence > 0.5


def test_live_router_build_task():
    r = _router()
    d = r.classify("build me a small python script that sorts files")
    assert d.workflow_id == "build"
    assert d.confidence > 0.5
