
"""Hermetic doc-truth regression tests (slice 50 HARDEN).

slice-50 static gap-hunt: README/SUBMISSION carried stale test counts
(507/512 -> 568/573) and the README repository-layout tree pointed
learn/ chains/ workflows/ at the repo ROOT when they actually live
under nine/ (the root workflows/ dir is a leftover demo script, not
the workflow-DAG home). These tests pin the doc to reality so the
claims cannot drift again.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

def _readme() -> str:
    return (REPO / "README.md").read_text(encoding="utf-8")

def _submission() -> str:
    return (REPO / "SUBMISSION.md").read_text(encoding="utf-8")

def test_readme_test_counts_are_current():
    # The badge + roadmap + tree must not claim a stale suite size.
    # torture-29 F6: the 5 doc-truth tests THEMSELVES pushed the suite
    # 573 -> 578 collected, so the doc had to track reality again.
    t = _readme()
    assert "tests-617%20passing" in t, "badge stale (expected 617 passing)"
    assert "617 passing tests (622 collected" in t, "roadmap claim stale"
    assert "622 tests (router, ledger" in t, "tree count stale"

def test_submission_test_counts_are_current():
    s = _submission()
    assert "617/622 tests pass" in s, "submission loop claim stale"
    assert "617 tests (622 collected)" in s, "submission readiness claim stale"

def test_readme_layout_tree_points_at_real_paths():
    t = _readme()
    # The repository-layout block must reference actual directories.
    tree = t[t.find("Repository layout"):t.find("## Roadmap")]
    for entry in ("router/classifier.py", "ledger/ledger.py",
                  "gates/evidence.py", "schema_validation.py",
                  "runtime/workflows.py", "runtime/adk_runtime.py",
                  "cli.py", "schemas/", "nine/learn/", "nine/chains/",
                  "nine/workflows/", "deploy/", "docs/", "tests/"):
        assert entry in tree, f"layout tree missing/relocated {entry}"
        real = entry if entry.startswith("nine/") or entry in (
            "schemas/", "deploy/", "docs/", "tests/") else f"nine/{entry}"
        assert (REPO / real).exists(), f"layout claims nonexistent {real}"

def test_readme_tree_does_not_point_at_root_stubs():
    # learn/ chains/ are NOT repo-root dirs — only nine/* variants exist.
    t = _readme()
    tree = t[t.find("Repository layout"):t.find("## Roadmap")]
    assert "learn/" not in tree.replace("nine/learn/", ""),             "layout still claims a root learn/ dir"
    assert "chains/" not in tree.replace("nine/chains/", ""),             "layout still claims a root chains/ dir"

def test_no_stale_suite_numbers_anywhere_in_docs():
    # 507/512/548/481/486 are all historical suite sizes; none should
    # appear as a CURRENT claim (historical TRACKER rows are exempt).
    for doc in (_readme(), _submission()):
        assert "507" not in doc, "stale 507 count"
        assert "512" not in doc, "stale 512 count"
        assert "548" not in doc, "stale 548 count"
        assert "481" not in doc, "stale 481 count"
        assert "486" not in doc, "stale 486 count"
