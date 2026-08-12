"""CLI end-to-end tests (hermetic: keyword router, temp ledger).

Model-or-fail doctrine: no GEMINI_API_KEY; the model-backed hops the CLI
submits (research summarize, ADK build, Gemma teach, respond) run on
monkeypatched fakes via the autouse fixture.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

from nine.cli import main


@pytest.fixture(autouse=True)
def _isolated_catalog(tmp_path, monkeypatch):
    """Learn apply/revert writes the shared git-tracked catalog for real;
    point it at a temp file so tests never touch the repo catalog."""
    monkeypatch.setattr("nine.registry._CATALOG_PATH", tmp_path / "catalog.json")


@pytest.fixture(autouse=True)
def _fake_models(monkeypatch):
    """Hermetic model fakes (no offline fallbacks exist anymore)."""
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
        return {"output": "wrote solution.py"}

    monkeypatch.setattr(
        flagship, "_build_adk_node",
        lambda: Node(id="build", kind="tool", run=fake_build_run,
                     description="fake ADK node (hermetic test)"),
    )
    monkeypatch.setattr(
        "nine.runtime.gemma.gemma_generate",
        lambda prompt, model=None, api_key=None, timeout=90:
        "gate every hop on evidence before handoff.",
    )


def test_cli_help_ok():
    import pytest

    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0


def test_cli_submit_shipped(tmp_path):
    assert main(["--ledger", str(tmp_path / "ledger.jsonl"), "submit", "research the printing press"]) == 0


def test_cli_chain_flagship(tmp_path):
    assert main(["--ledger", str(tmp_path / "ledger.jsonl"), "chain", "flagship", "build a calculator"]) == 0


def test_cli_chain_demo_lane(tmp_path):
    assert main(["--ledger", str(tmp_path / "ledger.jsonl"), "chain", "demo", "refund a customer"]) == 0


def test_cli_stats(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    assert main(["--ledger", str(ledger), "submit", "study black holes"]) == 0
    assert main(["--ledger", str(ledger), "stats"]) == 0


def test_cli_discover_and_cancel(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    assert main(["--ledger", str(ledger), "submit", "research x"]) == 0
    assert main(["--ledger", str(ledger), "discover"]) == 0
    assert main(["--ledger", str(ledger), "discover", "--status", "shipped"]) == 0


def test_cli_bad_command_returns_nonzero(tmp_path):
    import pytest

    with pytest.raises(SystemExit) as e:
        main(["--ledger", str(tmp_path / "ledger.jsonl"), "bogus"])
    assert e.value.code == 2

# ---------------------------------------------------------------- P2 learn CLI

def test_cli_submit_records_route_event(tmp_path, monkeypatch):
    """Every submit path writes a durable route event (was: zero events from
    the CLI; strategy claim-audit #5)."""
    ledger = tmp_path / "ledger.jsonl"
    events = tmp_path / "events.jsonl"
    assert main(["--ledger", str(ledger), "--events", str(events),
                 "submit", "research black holes"]) == 0
    r = main(["--ledger", str(ledger), "--events", str(events), "learn", "events"])
    assert r == 0
    lines = open(events).read().splitlines()
    assert len(lines) == 1
    import json
    assert json.loads(lines[0])["workflow_id"] == "research"
    assert json.loads(lines[0])["verdict"] == "SHIP"


def test_cli_unknown_task_runs_respond_workflow(tmp_path):
    """Unknown tasks are real jobs: routed to respond, verified SHIP."""
    ledger = tmp_path / "ledger.jsonl"
    events = tmp_path / "events.jsonl"
    assert main(["--ledger", str(ledger), "--events", str(events),
                 "submit", "zzz qqq unknown"]) == 0
    import json
    lines = open(events).read().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["workflow_id"] == "respond"
    assert json.loads(lines[0])["verdict"] == "SHIP"


def test_cli_learn_apply_revert_gated_by_regression(tmp_path, monkeypatch):
    """apply runs the regression gate before+after, writes the git-tracked
    catalog, and reverts cleanly — with the gate+git monkeypatched so the
    unit test never spawns pytest or commits."""

    import nine.cli as cli

    events = tmp_path / "events.jsonl"
    ledger = tmp_path / "ledger.jsonl"
    # record a low-confidence route -> keyword candidate (distinctive token
    # so the assertion survives a catalog that already routes 'chromodynamics')
    assert main(["--ledger", str(ledger), "--events", str(events),
                 "submit", "study fooquark dynamics"]) == 0
    out = main(["--ledger", str(ledger), "--events", str(events), "learn", "scan"])
    assert out == 0
    cands = cli._learner(type("A", (), {"events": str(events)})()).cands.all()
    assert len(cands) == 1 and cands[0].kind == "keyword"
    cid = cands[0].candidate_id

    gate_calls = {"n": 0}

    def _fake_green() -> bool:
        gate_calls["n"] += 1
        return True

    monkeypatch.setattr(cli, "_regression_green", _fake_green)
    commits = []
    monkeypatch.setattr(cli, "_git_commit", commits.append)

    from nine.registry import load_catalog
    # apply: catalog gains the keyword; candidate applied
    assert cli._apply_candidate(cli._learner(type("A", (), {"events": str(events)})()), cid) == 0
    cat = load_catalog()
    assert "fooquark" in cat["keyword_overrides"]["research"]
    assert cli._learner(type("A", (), {"events": str(events)})()).cands.get(cid).status == "applied"
    assert len(commits) == 1 and "fooquark" in commits[0]
    assert gate_calls["n"] >= 2  # pre + post change

    # revert: catalog loses the keyword; candidate back to pending
    assert cli._revert_candidate(cli._learner(type("A", (), {"events": str(events)})()), cid) == 0
    cat = load_catalog()
    assert "keyword_overrides" not in cat or not cat["keyword_overrides"].get("research")
    assert cli._learner(type("A", (), {"events": str(events)})()).cands.get(cid).status == "pending"
    assert len(commits) == 2


def test_cli_learn_apply_refuses_non_applicable(tmp_path, monkeypatch):
    """A candidate with no actionable keyword (all stopwords) cannot
    auto-apply -> apply refuses (no catalog write in tests)."""
    import nine.cli as cli

    events = tmp_path / "events.jsonl"
    ledger = tmp_path / "ledger.jsonl"
    assert main(["--ledger", str(ledger), "--events", str(events),
                 "submit", "make this task for them please"]) == 0
    assert main(["--ledger", str(ledger), "--events", str(events), "learn", "scan"]) == 0
    cands = cli._learner(type("A", (), {"events": str(events)})()).cands.all()
    assert len(cands) == 1 and cands[0].params["keyword"] == ""
    monkeypatch.setattr(cli, "_regression_green", lambda: True)
    assert cli._apply_candidate(cli._learner(type("A", (), {"events": str(events)})()), cands[0].candidate_id) == 2
