"""兼容部分 OpenAI 端点把数组参数序列化为 JSON 字符串。"""

from __future__ import annotations

import json

from codeguard_agent.models.council import CandidateInvestigationPlan
from codeguard_agent.pipeline.council.concern import _ParsedClaimBatch
from codeguard_agent.pipeline.evidence.agent import (
    _EvidenceAnalysis,
    _EvidenceAnalysisBatch,
    _RawFact,
    _finding_from_analysis,
)
from codeguard_agent.pipeline.evidence.researcher import _EscalationOutput
from codeguard_agent.pipeline.evidence.strategist import _StrategyOutput


def _plan_payload():
    return {
        "candidate_id": "candidate-1",
        "hypothesis": "外部输入到达危险调用",
        "questions": json.dumps(
            [
                {
                    "purpose": "support",
                    "question": "输入是否可达？",
                    "why_it_matters": "验证错误机制",
                    "expected_fact": "reachability",
                }
            ],
            ensure_ascii=False,
        ),
    }


def test_all_agentic_batch_models_accept_stringified_array_fields():
    encoded_plans = json.dumps([_plan_payload()], ensure_ascii=False)

    strategy = _StrategyOutput.model_validate({"plans": encoded_plans})
    escalation = _EscalationOutput.model_validate({"plans": encoded_plans})
    strategy_plan = CandidateInvestigationPlan.model_validate(strategy.plans[0])
    escalation_plan = CandidateInvestigationPlan.model_validate(
        escalation.plans[0]
    )

    assert strategy_plan.questions[0].question == "输入是否可达？"
    assert escalation_plan.candidate_id == "candidate-1"
    assert CandidateInvestigationPlan.model_validate(
        _plan_payload()
    ).questions


def test_agentic_batch_keeps_valid_raw_plans_when_a_sibling_is_malformed():
    valid = _plan_payload()
    malformed = {
        **_plan_payload(),
        "candidate_id": "candidate-bad",
        "questions": [
            {
                "purpose": "support",
                "question": "缺少 expected_fact",
                "why_it_matters": "模型字段不完整",
            }
        ],
    }
    wrapped = json.dumps({"plans": [valid, malformed]}, ensure_ascii=False)

    output = _StrategyOutput.model_validate({"plans": wrapped})

    assert len(output.plans) == 2
    assert isinstance(output.plans[0], (CandidateInvestigationPlan, dict))


def test_existing_claim_and_finding_batches_accept_stringified_arrays():
    claims = _ParsedClaimBatch.model_validate(
        {
            "claims": json.dumps(
                [{"candidate_id": "candidate-1", "root_cause": "错误机制"}],
                ensure_ascii=False,
            )
        }
    )
    findings = _EvidenceAnalysisBatch.model_validate(
        {
            "findings": json.dumps(
                [
                    {
                        "evidence_id": "fact-1",
                        "relation": "supports",
                        "strength": "direct",
                        "observation": "代码直接调用危险 API",
                    }
                ],
                ensure_ascii=False,
            )
        }
    )

    assert claims.claims[0].candidate_id == "candidate-1"
    assert findings.findings[0].evidence_id == "fact-1"


def test_nonempty_relation_without_observation_downgrades_to_insufficient():
    finding = _finding_from_analysis(
        request=object(),  # request is unused by this early safety branch
        fact=_RawFact("fact-1", "task_patch", "+dangerousCall(input)"),
        result=_EvidenceAnalysis(
            evidence_id="fact-1",
            relation="supports",
            strength="direct",
            observation="",
        ),
    )

    assert finding.relation == "insufficient"
    assert finding.limitation == "analyst_missing_observation"
