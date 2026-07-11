"""Tests for deterministic RiskTag-scoped knowledge loading."""

from __future__ import annotations

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.knowledge_rules import load_knowledge


def test_load_knowledge_empty_tags_returns_empty_string() -> None:
    assert load_knowledge("threat_model", []) == ""


def test_load_knowledge_concatenates_matched_tags(tmp_path, monkeypatch) -> None:
    domain_dir = tmp_path / "threat_model"
    domain_dir.mkdir(parents=True)
    (domain_dir / "AUTHORIZATION.txt").write_text("AUTH_CONTENT", encoding="utf-8")
    (domain_dir / "INJECTION.txt").write_text("INJECTION_CONTENT", encoding="utf-8")

    import codeguard_agent.pipeline.knowledge_rules as knowledge_rules

    monkeypatch.setattr(knowledge_rules, "_KNOWLEDGE_DIR", tmp_path)

    assert load_knowledge(
        "threat_model", [RiskTag.AUTHORIZATION, RiskTag.INJECTION]
    ) == "AUTH_CONTENT\n\nINJECTION_CONTENT"


def test_load_knowledge_skips_missing_files_silently(tmp_path, monkeypatch) -> None:
    (tmp_path / "threat_model").mkdir(parents=True)
    import codeguard_agent.pipeline.knowledge_rules as knowledge_rules

    monkeypatch.setattr(knowledge_rules, "_KNOWLEDGE_DIR", tmp_path)

    assert load_knowledge("threat_model", [RiskTag.AUTHORIZATION]) == ""


def test_load_knowledge_uses_enum_order_deduplicates_and_skips_general_review(
    tmp_path, monkeypatch
) -> None:
    domain_dir = tmp_path / "behavior"
    domain_dir.mkdir(parents=True)
    (domain_dir / "AUTHORIZATION.txt").write_text("AUTH", encoding="utf-8")
    (domain_dir / "INJECTION.txt").write_text("INJECTION", encoding="utf-8")
    (domain_dir / "GENERAL_REVIEW.txt").write_text("GENERAL", encoding="utf-8")
    import codeguard_agent.pipeline.knowledge_rules as knowledge_rules

    monkeypatch.setattr(knowledge_rules, "_KNOWLEDGE_DIR", tmp_path)

    result = load_knowledge(
        "behavior",
        [
            RiskTag.INJECTION,
            RiskTag.GENERAL_REVIEW,
            RiskTag.AUTHORIZATION,
            RiskTag.INJECTION,
        ],
    )

    assert result == "AUTH\n\nINJECTION"
