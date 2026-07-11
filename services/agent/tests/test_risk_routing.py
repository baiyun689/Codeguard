"""Phase 2 RiskTag-to-reviewer routing tests."""

from __future__ import annotations

from codeguard_agent.models.tasks import RiskProfile, RiskSignal, RiskTag, ReviewTask, TaskSelection
from codeguard_agent.pipeline.risk_routing import (
    decide_tier,
    render_task_scope,
    reviewers_for_profile,
    routed_task_ids,
)


def _profile(task_id: str, *tags: RiskTag) -> RiskProfile:
    return RiskProfile(
        task_id=task_id,
        tag_scores={tag: 1 for tag in tags},
        signals=[
            RiskSignal(
                tag=tag,
                score=1,
                source=f"text:added:{tag.value.lower()}",
                reason=f"reason-{tag.value.lower()}",
            )
            for tag in tags
        ],
    )


def test_reviewers_for_profile_uses_fixed_tag_mapping():
    assert reviewers_for_profile(_profile("auth", RiskTag.AUTHORIZATION)) == frozenset(
        {"ThreatModelAgent", "BehaviorAgent"}
    )
    assert reviewers_for_profile(_profile("sql", RiskTag.SQL_DATA_ACCESS)) == frozenset(
        {"BehaviorAgent"}
    )
    assert reviewers_for_profile(_profile("general", RiskTag.GENERAL_REVIEW)) == frozenset(
        {"ThreatModelAgent", "BehaviorAgent", "MaintainabilityAgent"}
    )


def test_routed_task_ids_unions_tags_without_duplicates_and_skips_unselected():
    tasks = [
        ReviewTask(id="auth", file="Auth.java", patch="+auth"),
        ReviewTask(id="sql", file="Order.java", patch="+sql"),
        ReviewTask(id="general", file="Other.java", patch="+other"),
    ]
    profiles = {
        "auth": _profile("auth", RiskTag.AUTHORIZATION, RiskTag.INPUT_VALIDATION),
        "sql": _profile("sql", RiskTag.SQL_DATA_ACCESS),
        "general": _profile("general", RiskTag.GENERAL_REVIEW),
    }
    selection = TaskSelection(selected_task_ids=["auth", "sql"])

    assert routed_task_ids("ThreatModelAgent", tasks, profiles, selection) == ("auth",)
    assert routed_task_ids("BehaviorAgent", tasks, profiles, selection) == ("auth", "sql")
    assert routed_task_ids("MaintainabilityAgent", tasks, profiles, selection) == ()


def test_render_task_scope_contains_only_routed_selected_tasks_and_is_stable():
    tasks = [
        ReviewTask(id="auth", file="Auth.java", patch="+check auth"),
        ReviewTask(id="sql", file="Order.java", patch="+query sql"),
    ]
    profiles = {
        "auth": _profile("auth", RiskTag.AUTHORIZATION),
        "sql": _profile("sql", RiskTag.SQL_DATA_ACCESS),
    }
    selection = TaskSelection(selected_task_ids=["auth", "sql"])

    first = render_task_scope("behavior", tasks, profiles, selection)
    second = render_task_scope("behavior", tasks, profiles, selection)

    assert first == second
    assert '<review_scope reviewer="behavior">' in first
    assert 'id="auth"' in first
    assert "AUTHORIZATION" in first
    assert "+check auth" in first
    assert "query sql" in first
    assert 'id="sql"' in first

    threat_scope = render_task_scope("threat_model", tasks, profiles, selection)
    assert 'id="auth"' in threat_scope
    assert 'id="sql"' not in threat_scope


def test_decide_tier_react_when_any_tag_score_at_least_two():
    profile = RiskProfile(
        task_id="t1",
        tag_scores={RiskTag.RESOURCE_LIFECYCLE: 2},
    )
    assert decide_tier(profile) == "react"


def test_decide_tier_direct_when_only_weak_signal():
    profile = RiskProfile(
        task_id="t1",
        tag_scores={RiskTag.SQL_DATA_ACCESS: 1},
    )
    assert decide_tier(profile) == "direct"


def test_decide_tier_direct_for_general_review():
    profile = RiskProfile(
        task_id="t1",
        tag_scores={RiskTag.GENERAL_REVIEW: 1},
    )
    assert decide_tier(profile) == "direct"


def test_decide_tier_react_when_strong_signal_present():
    profile = RiskProfile(
        task_id="t1",
        tag_scores={RiskTag.AUTHORIZATION: 3},
    )
    assert decide_tier(profile) == "react"


def test_decide_tier_direct_when_profile_missing():
    assert decide_tier(None) == "direct"
