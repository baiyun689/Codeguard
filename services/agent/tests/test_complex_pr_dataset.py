from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml  # type: ignore[import-untyped]


DATASET_ROOT = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "dataset"
    / "repo"
    / "complex-pr-v1"
)
EXPECTED_MODULES = {
    "tradeflow-domain",
    "tradeflow-application",
    "tradeflow-web",
    "tradeflow-persistence",
    "tradeflow-integrations",
    "tradeflow-worker",
    "tradeflow-boot",
}
VALID_COVERAGE = {"exact", "composite", "gap"}
VALID_GAPS = {"BUSINESS_INVARIANT", "NUMERIC_MONEY", "TEMPORAL_SEMANTICS"}
BANNED_REVIEW_HINTS = {
    "todo",
    "fixme",
    "intentional bug",
    "deliberate flaw",
    "vulnerable",
    "unsafe",
    "故意",
    "这里有问题",
}


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_complex_pr_dataset_is_a_complete_twenty_pr_benchmark() -> None:
    manifest = _load_yaml(DATASET_ROOT / "manifest.yaml")
    case_dirs = sorted((DATASET_ROOT / "cases").glob("pr-*"))

    assert manifest["id"] == "complex-pr-v1"
    assert manifest["project"] == "TradeFlow"
    assert manifest["case_count"] == 20
    assert manifest["issue_count"] == 60
    assert len(case_dirs) == 20

    coverage_counts: Counter[str] = Counter()
    routing_hazards = 0
    knowledge_root = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "codeguard_agent"
        / "prompts"
        / "knowledge"
    )

    from codeguard_agent.models.tasks import RiskTag  # type: ignore[import-untyped]

    valid_tags = {tag.value for tag in RiskTag}

    for case_dir in case_dirs:
        case = _load_yaml(case_dir / "case.yaml")
        truth = _load_yaml(case_dir / "ground-truth.yaml")
        repo = case_dir / "repo"
        oracle_tests = sorted((case_dir / "oracle-tests").glob("*Test.java"))

        assert case["id"] == case_dir.name
        assert len(case["expected"]) == 3
        assert truth["case_id"] == case["id"]
        assert len(truth["issues"]) == 3
        assert len(oracle_tests) == 3
        assert (case_dir / "changes.diff").stat().st_size > 0
        assert (repo / "pom.xml").is_file()
        assert not (repo / "ground-truth.yaml").exists()
        assert not (repo / "oracle-tests").exists()
        assert EXPECTED_MODULES <= {
            path.name for path in repo.iterdir() if path.is_dir()
        }
        reviewed_text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in repo.rglob("*")
            if path.is_file()
        ).lower()
        assert not {
            marker for marker in BANNED_REVIEW_HINTS if marker in reviewed_text
        }

        changed_lines = sum(
            1
            for line in (case_dir / "changes.diff").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.startswith(("+", "-"))
            and not line.startswith(("+++", "---"))
        )
        assert 150 <= changed_lines <= 600

        issue_ids = {issue["id"] for issue in truth["issues"]}
        assert issue_ids == {
            expected["id"] for expected in case["expected"]
        }

        for issue in truth["issues"]:
            assert issue["risk_coverage"] in VALID_COVERAGE
            assert issue["primary_risk_tag"]
            assert issue["primary_risk_tag"] in valid_tags
            assert set(issue["secondary_risk_tags"]) <= valid_tags
            assert issue["expected_reviewers"]
            assert issue["required_knowledge"]
            assert len(issue["call_path"]) >= 3
            assert issue["trigger"]
            assert issue["observable_consequence"]
            assert issue["fix_location"]
            assert issue["fix_action"]
            assert issue["why_independent"]
            assert issue["oracle_test"]

            source = repo / issue["file"]
            assert source.is_file()
            source_lines = source.read_text(encoding="utf-8").splitlines()
            assert 0 < issue["line"] <= len(source_lines)
            assert issue["oracle_test"] in {path.name for path in oracle_tests}
            for fragment in issue["required_knowledge"]:
                domain, tag = fragment.split("/", maxsplit=1)
                if tag == "GENERAL_REVIEW":
                    continue
                assert (knowledge_root / domain / f"{tag}.txt").is_file()

            if issue["risk_coverage"] == "gap":
                assert issue["taxonomy_gap"] in VALID_GAPS
            else:
                assert issue["taxonomy_gap"] is None

            coverage_counts[issue["risk_coverage"]] += 1
            routing_hazards += int(issue["routing_hazard"])

    assert coverage_counts == Counter(exact=36, composite=16, gap=8)
    assert routing_hazards >= 10
