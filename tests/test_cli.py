"""CLI end-to-end tests (hermetic: keyword router, temp ledger)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chowlite.cli import main


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
