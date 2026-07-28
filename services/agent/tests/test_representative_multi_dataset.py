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
SOURCE_HASHES = {
    "gitbug-spring-guice-injection": "281b4e52172664030eea2ad2dce7cc5320689470f6b158aa109ef327f775bb1f",
    "vul4j-42-command-injection": "9cf29e0d6673ab66ed5c30ffbae4a8c903b835e7c31e01b9b246d2327dba1ff8",
    "vul4j-43-path-traversal": "66ad0dde4c5eea7fd84b740ee3e981c73a5186ebf539bf1562b879e5ab700230",
    "vul4j-48-jwt-validation": "486e46cbd2c1b53bf53557585a6a11ff6f66cb1736cd16248cd4c3309a11bcba",
    "gitbug-spring-retry-interrupt": "f1c28d8b6c5267acb11b32adb0d6abf2aab5eda50b92cc4ea1303e601585a634",
    "gitbug-snowflake-credentials": "ffcc48e3b0a7e40fdcc9338c725da22e1f41c7f5d461171b98a349208b0e8478",
    "gitbug-evalex-memory": "68d2cebb7a4dea7212570cb1bb4db78ab2f7bd3d150416f6406e8ee7b84b7344",
    "gitbug-mcs-runtime-errors": "5536ed2795347d2b198c6fe1734fb4caaa9536647c5f92fc836f22fde4141118",
    "gitbug-jaxb-uppercase": "86ba66eab0f90d12abd82acce75195434581583661bcd242ad985ae90959622f",
    "gitbug-quality-cbor-type": "cf3f54b8b6bc0a7f9deccb18d125a2f39df7f89c98de4f35472a8ae35068b665",
}


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_representative_multi_dataset_has_ten_real_multi_issue_cases() -> None:
    manifest = _yaml(ROOT / "manifest.yaml")
    cases = sorted((ROOT / "cases").glob("*"))

    assert manifest["id"] == "representative-multi-v1"
    assert manifest["case_count"] == 10
    assert manifest["issue_count"] == 30
    assert {
        case["id"]: case["source_snapshot_sha256"]
        for case in manifest["cases"]
    } == SOURCE_HASHES
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
