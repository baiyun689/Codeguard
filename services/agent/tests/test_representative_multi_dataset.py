from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from codeguard_agent.git.diff_collector import parse_changed_files
from evals.dataset import load_cases


ROOT = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "dataset"
    / "repo"
    / "representative-multi-v1"
)
BANNED_HINTS = {
    "todo",
    "fixme",
    "intentional bug",
    "deliberate flaw",
    "vulnerable",
    "unsafe",
    "seeded bug",
    "故意",
    "漏洞",
    "这里有问题",
}


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_representative_multi_dataset_has_ten_real_multi_issue_cases() -> None:
    manifest = _yaml(ROOT / "manifest.yaml")
    cases = sorted((ROOT / "cases").glob("*"))

    assert manifest["id"] == "representative-multi-v1"
    assert manifest["case_count"] == 10
    assert manifest["issue_count"] == 30
    assert all(
        len(case["source_snapshot_sha256"]) == 64
        for case in manifest["cases"]
    )
    assert len(cases) == 10

    repositories: set[str] = set()
    dimensions: Counter[str] = Counter()
    for case_dir in cases:
        case = _yaml(case_dir / "case.yaml")
        truth = _yaml(case_dir / "ground-truth.yaml")
        diff = (case_dir / "changes.diff").read_text(encoding="utf-8")
        repo = case_dir / "repo"

        assert case["id"] == case_dir.name
        assert case["ground_truth_mode"] == "known-issue-only"
        assert len(case["expected"]) == 3
        assert len(truth["issues"]) == 3
        assert repo.is_dir()
        assert diff.startswith("diff --git ")
        assert diff.count("diff --git ") >= 2
        assert any(repo.rglob("*.java"))
        assert not (repo / "oracle-tests").exists()
        changed_files = set(parse_changed_files(diff))
        assert {
            issue["file"] for issue in truth["issues"]
        } <= changed_files
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "apply",
                "--check",
                "--reverse",
                str((case_dir / "changes.diff").resolve()),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        seeded_diff = (case_dir / "seeded.diff").read_text(
            encoding="utf-8"
        ).lower()
        seeded_additions = "\n".join(
            line[1:]
            for line in seeded_diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        assert not {hint for hint in BANNED_HINTS if hint in seeded_additions}
        assert not any(
            marker in line
            for line in seeded_additions.splitlines()
            for marker in ("//", "/*", "*/")
        )

        repositories.add(case["provenance"]["repository_url"])
        issue_ids = {issue["id"] for issue in truth["issues"]}
        assert issue_ids == {issue["id"] for issue in case["expected"]}
        assert all(issue["line"] > 0 for issue in truth["issues"])
        oracle_files = sorted((case_dir / "oracle-tests").glob("*.yaml"))
        assert len(oracle_files) == 3
        oracles = [_yaml(path) for path in oracle_files]
        assert {oracle["issue_id"] for oracle in oracles} == issue_ids
        for oracle in oracles:
            issue = next(
                item for item in truth["issues"] if item["id"] == oracle["issue_id"]
            )
            assert oracle["not_exposed_to_review_model"] is True
            assert oracle["oracle_type"] == "declarative-regression-contract"
            assert oracle["trigger"] == issue["trigger"]
            assert (
                oracle["expected_observation"]
                == issue["observable_consequence"]
            )
            assert oracle["source_anchor"] == {
                "file": issue["file"],
                "line": issue["line"],
                "call_path": issue["call_path"],
            }
        assert sum(issue["origin"] == "upstream-real" for issue in truth["issues"]) == 1
        assert sum(issue["origin"] == "controlled-seed" for issue in truth["issues"]) == 2

        fixes = {issue["fix_action"] for issue in truth["issues"]}
        assert len(fixes) == 3
        for issue in truth["issues"]:
            dimensions[issue["dimension"]] += 1
            assert issue["root_cause"]
            assert issue["trigger"]
            assert issue["observable_consequence"]
            assert issue["file"]
            assert issue["call_path"]
            assert issue["primary_risk_tag"]
            assert issue["expected_reviewers"]
            assert (repo / issue["file"]).is_file()

    assert len(repositories) == 10
    assert set(dimensions) == {"security", "logic", "quality"}

    loaded = load_cases(ROOT)
    assert len(loaded) == 10
    assert all(case.repo_path for case in loaded)
    assert all(case.ground_truth_mode == "known-issue-only" for case in loaded)
