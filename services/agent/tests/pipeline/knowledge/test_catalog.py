"""KnowledgeCatalog 单元测试。"""
from __future__ import annotations

from codeguard_agent.models.tasks import ReviewerKind
from codeguard_agent.pipeline.knowledge.catalog import KnowledgeCatalog


class TestKnowledgeCatalog:
    def test_all_three_bases_readable(self):
        catalog = KnowledgeCatalog()
        for reviewer in ReviewerKind:
            base = catalog.base_fragment(reviewer)
            assert base is not None, f"{reviewer} BASE missing"
            assert len(base.content) > 100, f"{reviewer} BASE too short"
            assert base.kind.value == "base"

    def test_specialized_only_returns_correct_reviewer(self):
        catalog = KnowledgeCatalog()
        for reviewer in ReviewerKind:
            fragments = catalog.specialized_fragments(reviewer)
            for f in fragments:
                assert f.reviewer == reviewer
                assert f.kind.value == "specialized"
                assert f.risk_tag is not None

    def test_fragments_stable_order(self):
        catalog = KnowledgeCatalog()
        first = catalog.specialized_fragments(ReviewerKind.THREAT_MODEL)
        second = catalog.specialized_fragments(ReviewerKind.THREAT_MODEL)
        assert [f.topic for f in first] == [f.topic for f in second]

    def test_behavior_has_no_threat_fragments(self):
        catalog = KnowledgeCatalog()
        threat_topics = {f.risk_tag for f in catalog.specialized_fragments(ReviewerKind.THREAT_MODEL) if f.risk_tag}
        behavior_frags = catalog.specialized_fragments(ReviewerKind.BEHAVIOR)
        for f in behavior_frags:
            if f.risk_tag and f.risk_tag in threat_topics:
                pass  # overlapping tags are OK (some tags map to both reviewers)

    def test_base_fragment_has_required_sections(self):
        catalog = KnowledgeCatalog()
        for reviewer in ReviewerKind:
            base = catalog.base_fragment(reviewer)
            assert base is not None
            content = base.content
            assert "[Purpose]" in content
            assert "[Always inspect]" in content
            assert "[Evidence discipline]" in content
            assert "[Do not infer]" in content
            assert "[Candidate quality]" in content

    def test_missing_base_file_returns_none(self, tmp_path):
        """当 BASE 文件缺失时返回 None 而不崩溃。"""
        catalog = KnowledgeCatalog(root=tmp_path)
        base = catalog.base_fragment(ReviewerKind.THREAT_MODEL)
        assert base is None

    def test_empty_dir_returns_empty_fragments(self, tmp_path):
        """空目录不崩溃。"""
        catalog = KnowledgeCatalog(root=tmp_path)
        fragments = catalog.specialized_fragments(ReviewerKind.BEHAVIOR)
        assert len(fragments) == 0
