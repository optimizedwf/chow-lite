"""bench_nine.py CLI-guard tests — a typo'd flag must NEVER start a live
Gemini bench (slice-49: `--help` silently ran the full set and burned quota).

These tests run the parser in-process; none of them invoke the actual
benchmark loop (no Gemini quota, no subprocess, no .venv/bin/nine).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))  # noqa: E402

import bench_nine as bn


def _parse(argv):
    """Run the parser and return (result, out, err)."""
    import io
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        res = bn._parse_args(argv)
        return res, sys.stdout.getvalue(), sys.stderr.getvalue()
    finally:
        sys.stdout, sys.stderr = old_out, old_err


def test_help_returns_none_not_false():
    res, out, _ = _parse(["--help"])
    assert res is None
    assert "bench_nine.py" in out
    assert "--dry-run" in out


def test_unknown_flag_aborts_with_usage():
    res, out, err = _parse(["--frobnicate"])
    assert res is False
    assert "unknown argument" in err
    assert "usage:" in err


def test_unknown_flag_never_treated_as_bench():
    """The footgun: any typo aborts (rc 2) — it must NOT fall through to a
    live bench run."""
    res, out, err = _parse(["--helpp", "-x", "--task"])
    assert res is False
    assert "unknown argument" in err


def test_fixtures_parsed_and_zero_padded():
    res, out, _ = _parse(["--fixtures", "2,5,11"])
    assert res is not None and res is not False
    bn._apply_overrides(res)
    assert bn.FIXTURES == ["bugfix-small-002", "bugfix-small-005", "bugfix-small-011"]


def test_fixtures_missing_value_errors():
    res, out, err = _parse(["--fixtures"])
    assert res is False
    assert "requires a comma-separated list" in err


def test_task_mode_valid_and_invalid():
    res, _, _ = _parse(["--task-mode", "desc"])
    assert res is not None and res is not False
    assert res["task_mode"] == "desc"
    res2, _, err2 = _parse(["--task-mode", "bogus"])
    assert res2 is False
    assert "must be full|desc" in err2


def test_runid_and_list_and_dry_run_flags():
    res, _, _ = _parse(["--runid", "gem-r2"])
    assert res["runid"] == "gem-r2"
    res2, _, _ = _parse(["--list"])
    assert res2 == {"list": True}
    res3, _, _ = _parse(["--dry-run"])
    assert res3 == {"dry-run": True}


def test_combined_flags_parse():
    res, _, _ = _parse(["--fixtures", "001,005", "--runid", "x1", "--task-mode", "desc"])
    assert res["fixtures"] == ["001", "005"]
    assert res["runid"] == "x1"
    assert res["task_mode"] == "desc"


def test_default_fixtures_discovered_from_disk_not_hardcoded():
    """The DEFAULT fixture set must equal what is actually on disk — a new
    fixture (bugfix-small-012+) must bench by default, a deleted one must
    not silently shrink the scoreboard (bench_nine.py used to hardcode
    range(1, 12); slice-56 switched to disk discovery).

    NOTE: other tests in this file call _apply_overrides() which mutates
    bn.FIXTURES, so this test re-imports the module fresh.
    """
    import importlib

    import bench_nine as bn

    bn = importlib.reload(bn)  # drop any overrides applied by earlier tests

    on_disk = sorted(
        p.name for p in bn.FIXTURES_DIR.iterdir()
        if p.is_dir() and p.name.startswith("bugfix-small-")
    )
    assert bn.FIXTURES == on_disk, (
        f"default FIXTURES {bn.FIXTURES} drifts from disk {on_disk}"
    )
    # sanity: 001 is present so discovery is actually reading the dir
    assert "bugfix-small-001" in on_disk
