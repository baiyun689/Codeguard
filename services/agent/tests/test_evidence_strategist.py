"""EvidenceStrategist 的动态问题规划契约。"""

from __future__ import annotations

from codeguard_agent.models.council import (
    CandidateClaim,
    CandidateConcern,
    CandidateInvestigationPlan,
    InvestigationQuestion,
)
from codeguard_agent.pipeline.evidence.strategist import (
    build_investigation_plans,
    investigation_plans_to_requests,
)
from codeguard_agent.pipeline.council import concern as _concern_model_setup  # noqa: F401


def _concern(candidate_id: str = "candidate-1") -> CandidateConcern:
    return CandidateConcern(
        member_candidate_ids=(candidate_id,),
        claims=(
            CandidateClaim(
                candidate_id=candidate_id,
                root_cause="外部参数未经转义就拼接到命令中",
                trigger="HTTP 参数包含 shell 元字符",
                observable_consequence="攻击者可执行任意系统命令",
                fix_location="src/Command.java:18",
            ),
        ),
        files=("src/Command.java",),
    )


class _Structured:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return self.result


class _Llm:
    def __init__(self, result):
        self.structured = _Structured(result)
        self.schema = None

    def with_structured_output(self, schema, method):
        self.schema = schema
        assert method == "function_calling"
        return self.structured


def _plan(candidate_id: str) -> CandidateInvestigationPlan:
    return CandidateInvestigationPlan(
        candidate_id=candidate_id,
        hypothesis="外部输入可能到达命令执行点",
        questions=(
            InvestigationQuestion(
                purpose="support",
                question="变更后的命令字符串是否包含未经净化的外部参数？",
                why_it_matters="决定命令注入机制是否成立",
                expected_fact="data_flow",
            ),
            InvestigationQuestion(
                purpose="counter",
                question="调用路径上是否存在严格白名单或安全参数化执行？",
                why_it_matters="有效保护会推翻候选",
                expected_fact="guard_presence",
            ),
        ),
    )


def test_strategist_plans_batch_in_one_llm_call_and_preserves_dynamic_questions():
    concern = _concern()
    llm = _Llm({"plans": [_plan("candidate-1")]})

    batch = build_investigation_plans(
        [concern],
        llm=llm,
        structured_method="function_calling",
    )

    assert len(llm.structured.calls) == 1
    assert batch.plans == (_plan("candidate-1"),)
    assert batch.fallback_candidate_ids == ()
    requests = investigation_plans_to_requests(batch.plans, [concern])
    assert [request.question for request in requests] == [
        "变更后的命令字符串是否包含未经净化的外部参数？",
        "调用路径上是否存在严格白名单或安全参数化执行？",
    ]
    assert [request.strategy_id for request in requests] == [
        "claim.data_flow.support",
        "claim.guard_presence.counter",
    ]


def test_strategist_drops_unknown_ids_and_falls_back_only_for_missing_candidates():
    concerns = [_concern("candidate-1"), _concern("candidate-2")]
    llm = _Llm(
        {
            "plans": [
                _plan("unknown"),
                _plan("candidate-1"),
                _plan("candidate-1"),
            ]
        }
    )

    batch = build_investigation_plans(
        concerns,
        llm=llm,
        structured_method="function_calling",
    )

    assert [plan.candidate_id for plan in batch.plans] == [
        "candidate-1",
        "candidate-2",
    ]
    assert batch.plans[0].source == "llm"
    assert batch.plans[1].source == "fallback"
    assert batch.fallback_candidate_ids == ("candidate-2",)


def test_strategist_llm_failure_uses_small_nonempty_fallback_not_three_goal_bank():
    concern = _concern()

    batch = build_investigation_plans(
        [concern],
        llm=None,
        structured_method="function_calling",
    )

    assert len(batch.plans) == 1
    assert batch.plans[0].source == "fallback"
    assert 1 <= len(batch.plans[0].questions) <= 2
    requests = investigation_plans_to_requests(batch.plans, [concern])
    assert 1 <= len(requests) <= 2
    assert all(request.target == "src/Command.java" for request in requests)


def test_not_actionable_plan_creates_one_minimal_verification_request():
    concern = _concern()
    plan = CandidateInvestigationPlan(
        candidate_id="candidate-1",
        hypothesis="只是一项命名偏好",
        actionable=False,
        skip_reason="无需事实调查即可判断为非问题",
        source="llm",
    )

    requests = investigation_plans_to_requests([plan], [concern])
    assert len(requests) == 1
    assert requests[0].purpose == "support"
    assert requests[0].fact_type == "changed_condition"
