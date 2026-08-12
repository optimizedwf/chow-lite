"""ADK 2.0 integration test — requires GEMINI_API_KEY (skips otherwise).

Verifies the mandatory Google agent framework requirement end-to-end:
Gemini 3.5 Flash via ADK -> real tool call -> nine workflow engine
-> evidence gate -> SHIP.

Run:  GEMINI_API_KEY=... python -m pytest tests/test_adk.py -v
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY not set",
)


def test_adk_agent_tool_call_and_ship(tmp_path):
    from google.adk.agents import LlmAgent
    from google.adk.models import Gemini
    from google.adk.tools import FunctionTool

    from nine.gates.evidence import EvidenceGate, eval_json_check, exit_codes_check
    from nine.ledger.ledger import JSONLLedger
    from nine.runtime.adk_runtime import make_adk_node
    from nine.runtime.workflows import Node, Workflow, WorkflowExecutor

    def get_stock_price(ticker: str) -> str:
        """Look up the current price of a stock ticker."""
        prices = {"AAPL": 212.40, "MSFT": 448.10, "NVDA": 118.90, "TSLA": 249.60}
        return json.dumps({"ticker": ticker, "price": prices.get(ticker.upper(), "unknown")})

    agent = LlmAgent(
        name="market_agent",
        model=Gemini(model="gemini-3.6-flash"),
        instruction="You are a market research agent. Use tools. Be concise.",
        tools=[FunctionTool(get_stock_price)],
    )

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    gate.register_check("exit-codes", exit_codes_check())

    wf = Workflow(id="market_research")
    spec = make_adk_node(agent)
    wf.add_node(Node(id=spec["id"], kind="subagent", run=spec["run"]))
    wf.add_node(Node(
        id="verify", kind="bash",
        command=(
            "python3 -c \"import json; "
            "json.dump({'checks':[{'name':'agent-output','passed':True}]}, "
            "open('EVAL.json','w'))\"; "
            "test -s agent_output.md && echo ok > FINAL_REPORT.md"
        ),
        depends_on=[spec["id"]],
    ))

    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("market_research", {"task": "AAPL and NVDA prices?"})
    result = ex.execute(
        wf, job,
        {"task": "What is the current price of AAPL and NVDA?", "job_id": job.job_id},
    )

    assert result["verdict"]["verdict"] == "SHIP"
    assert ledger.get(job.job_id).status == "shipped"
    calls = result["node_outputs"]["market_agent"]["function_calls"]
    assert any("get_stock_price" in c for c in calls), f"no tool calls: {calls}"
    assert "agent_output.md" in {a["name"] for a in result["artifacts"]}
