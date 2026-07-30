"""EvidenceResearcher 快速路径与选择性 ReAct 契约。"""

from __future__ import annotations

from dataclasses import dataclass, field

from codeguard_agent.models.council import (
    CandidateClaim,
    CandidateConcern,
    CandidateInvestigationPlan,
    EvidenceFinding,
    EvidenceNote,
    InvestigationQuestion,
)
from codeguard_agent.pipeline.council import concern as _concern_model_setup  # noqa: F401
from codeguard_agent.pipeline.evidence.agent import EvidenceBatch
from codeguard_agent.pipeline.evidence.researcher import research_evidence
from codeguard_agent.pipeline.evidence.strategist import (
    investigation_plans_to_requests,
)
from codeguard_agent.tools.tool_client import ToolResponse


def _concern(candidate_id: str) -> CandidateConcern:
    return CandidateConcern(
        member_candidate_ids=(candidate_id,),
        claims=(
            CandidateClaim(
                candidate_id=candidate_id,
                root_cause="外部输入直接进入危险调用",
                trigger="HTTP 参数可控",
                observable_consequence="执行非预期命令",
                fix_location=f"src/{candidate_id}.java:10",
            ),
        ),
        files=(f"src/{candidate_id}.java",),
    )


def _plan(candidate_id: str) -> CandidateInvestigationPlan:
    return CandidateInvestigationPlan(
        candidate_id=candidate_id,
        hypothesis="外部输入到达危险调用",
        questions=(
            InvestigationQuestion(
                purpose="support",
                question="变更代码是否直接调用危险 API？",
                why_it_matters="验证错误机制",
                expected_fact="changed_condition",
            ),
        ),
    )


@dataclass
class _Collector:
    calls: list[list] = field(default_factory=list)

    def __call__(self, _dossiers, requests, **_kwargs):
        requests = list(requests)
        self.calls.append(requests)
        notes = []
        for request in requests:
            relation = "supports" if len(self.calls) == 2 else "insufficient"
            notes.append(
                EvidenceNote(
                    request_id=request.id,
                    candidate_id=request.candidate_id,
                    findings=[
                        EvidenceFinding(
                            evidence_id=f"fact-{request.id}",
                            source="task_patch",
                            observation="调用路径已确认" if relation == "supports" else "",
                            relation=relation,
                            strength="contextual",
                            limitation="" if relation == "supports" else "missing_call_path",
                        )
                    ],
                )
            )
        return EvidenceBatch(notes=notes)


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

    def with_structured_output(self, _schema, method):
        assert method == "function_calling"
        return self.structured


def test_researcher_only_escalates_difficult_candidates_and_stops_after_second_round():
    concerns = [_concern("candidate-1"), _concern("candidate-2")]
    plans = [_plan("candidate-1"), _plan("candidate-2")]
    initial_requests = investigation_plans_to_requests(plans, concerns)
    collector = _Collector()
    llm = _Llm(
        {
            "plans": [
                CandidateInvestigationPlan(
                    candidate_id="candidate-1",
                    hypothesis="需要确认入口可达性",
                    questions=(
                        InvestigationQuestion(
                            purpose="support",
                            question="该方法是否存在框架入口或上游调用方？",
                            why_it_matters="补齐可达性",
                            expected_fact="call_path",
                        ),
                    ),
                )
            ]
        }
    )

    batch = research_evidence(
        plans,
        concerns,
        dossiers=[],
        initial_requests=initial_requests,
        tool_client=object(),
        analyst_llm=llm,
        structured_method="function_calling",
        enabled_tools=["inspect_change_impact"],
        collect_fn=collector,
        max_react_candidates=1,
    )

    assert len(collector.calls) == 2
    assert {request.candidate_id for request in collector.calls[1]} == {"candidate-1"}
    summaries = {item.candidate_id: item for item in batch.dossier_summaries}
    assert summaries["candidate-1"].react_used is True
    assert summaries["candidate-1"].rounds == 2
    assert summaries["candidate-1"].status == "supported"
    assert summaries["candidate-2"].react_used is False
    assert summaries["candidate-2"].status == "insufficient"


def test_researcher_never_enters_react_without_tool_client():
    concern = _concern("candidate-1")
    plan = _plan("candidate-1")
    requests = investigation_plans_to_requests([plan], [concern])
    collector = _Collector()
    llm = _Llm({"plans": [_plan("candidate-1")]})

    batch = research_evidence(
        [plan],
        [concern],
        dossiers=[],
        initial_requests=requests,
        tool_client=None,
        analyst_llm=llm,
        structured_method="function_calling",
        enabled_tools=None,
        collect_fn=collector,
    )

    assert len(collector.calls) == 1
    assert len(llm.structured.calls) == 0
    assert batch.dossier_summaries[0].react_used is False
    assert "no_tool_client" in batch.dossier_summaries[0].limitations


def test_react_allows_more_specific_same_fact_question_and_marks_cross_round_cache():
    concern = _concern("candidate-1")
    plan = _plan("candidate-1")
    requests = investigation_plans_to_requests([plan], [concern])

    class _Client:
        calls = 0

        def get_file_content(self, file_path):
            self.calls += 1
            return ToolResponse(success=True, result=f"class X {{ // {file_path} }}")

    client = _Client()
    call_count = 0

    def collector(_dossiers, current_requests, **kwargs):
        nonlocal call_count
        call_count += 1
        request = list(current_requests)[0]
        kwargs["tool_client"].get_file_content(file_path=request.target)
        relation = "supports" if call_count == 2 else "insufficient"
        return EvidenceBatch(
            notes=[
                EvidenceNote(
                    request_id=request.id,
                    candidate_id=request.candidate_id,
                    findings=[
                        EvidenceFinding(
                            evidence_id=f"fact-{call_count}",
                            source="tool:get_file_content",
                            observation="具体调用已确认" if relation == "supports" else "",
                            relation=relation,
                            strength="contextual",
                            limitation="" if relation == "supports" else "too_broad",
                        )
                    ],
                )
            ],
            trace=[
                (
                    "evidence_tool_called",
                    (
                        '{"candidate_id":"candidate-1",'
                        '"tool":"get_file_content",'
                        f'"arguments":{{"file_path":"{request.target}"}}'
                        "}"
                    ),
                )
            ],
        )

    llm = _Llm(
        {
            "plans": [
                CandidateInvestigationPlan(
                    candidate_id="candidate-1",
                    hypothesis="需要检查具体调用表达式",
                    questions=(
                        InvestigationQuestion(
                            purpose="support",
                            question="第 10 行的实参是否直接来自请求参数？",
                            why_it_matters="比首轮宽泛问题更具体",
                            expected_fact="changed_condition",
                        ),
                    ),
                )
            ]
        }
    )

    batch = research_evidence(
        [plan],
        [concern],
        dossiers=[],
        initial_requests=requests,
        tool_client=client,
        analyst_llm=llm,
        structured_method="function_calling",
        enabled_tools=["get_file_content", "inspect_change_impact"],
        collect_fn=collector,
    )

    assert call_count == 2
    assert client.calls == 1
    assert any(
        event == "evidence_tool_reused_cross_round"
        for event, _detail in batch.trace
    )
