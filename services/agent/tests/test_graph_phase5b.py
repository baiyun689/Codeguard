"""Phase 5B graph wiring and single-writer contracts."""

from __future__ import annotations

from types import SimpleNamespace

import codeguard_agent.pipeline.graph as G
from codeguard_agent.models.council import CandidateIssue, EvidenceRequest, Verdict
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import ReviewTask, TaskSelection
from codeguard_agent.pipeline.stages.reviewer_stage import DEFAULT_REVIEWERS
from codeguard_agent.pipeline.evidence_rules import STRATEGIES_BY_ID
from codeguard_agent.tools.tool_client import ToolResponse


def _request(index: int) -> EvidenceRequest:
    return EvidenceRequest(
        candidate_id=f"candidate-{index}",
        strategy_id="general_review.counter",
        purpose="counter",
        target=f"src/Service{index}.java",
        question="检查候选主张的反证",
    )


def _candidate_task():
    task = ReviewTask(
        id="src/Service.java#h0",
        file="src/Service.java",
        hunk_header="@@ -10,1 +10,1 @@",
        patch="+changed();",
        changed_lines=[10],
    )
    candidate = CandidateIssue(
        id="candidate-1",
        task_id=task.id,
        source_agent="threat_model",
        file=task.file,
        line=10,
        type="general review",
        severity_proposal=Severity.WARNING,
        claim="candidate claim",
        confidence=0.8,
    )
    return candidate, task


def test_evidence_request_reducer_only_deduplicates_without_cap():
    requests = [_request(index) for index in range(30)]

    reduced = G.dedup_evidence_request_reducer([], [*requests, requests[0]])

    assert reduced == requests


def test_reviewer_branch_never_writes_evidence_requests():
    node = G.make_reviewer_node(DEFAULT_REVIEWERS[0], llm=None, tool_client=None)

    out = node(
        {
            "review_tasks": [],
            "risk_profiles": {},
            "task_selection": TaskSelection(selected_task_ids=[]),
        }
    )

    assert "evidence_requests" not in out


def test_planner_node_is_the_request_writer(monkeypatch):
    candidate, task = _candidate_task()
    planned = _request(99)
    monkeypatch.setattr(
        G,
        "plan_evidence",
        lambda *args, **kwargs: SimpleNamespace(
            requests=[planned],
            trace=[("evidence_planned", "{}")],
        ),
    )

    out = G._evidence_planner_node(None)(
        {
            "candidate_issues": [candidate],
            "review_tasks": [task],
            "risk_profiles": {},
            "task_context_bundles": {},
            "evidence_requests": [],
            "evidence_notes": [],
            "council_verdicts": [],
            "evidence_round": 0,
        }
    )

    assert out["evidence_requests"] == [planned]


def test_first_evidence_agent_noop_still_increments_round():
    out = G._evidence_agent_node(tool_client=None, judge_llm=None)(
        {
            "candidate_issues": [],
            "review_tasks": [],
            "evidence_requests": [],
            "evidence_notes": [],
            "evidence_round": 0,
        }
    )

    assert out["evidence_round"] == 1
    assert any(trace.event == "no_op" for trace in out["council_trace"])


def test_judge_needs_more_routes_back_to_planner():
    state = {
        "council_verdicts": [
            Verdict(
                candidate_id="candidate-1",
                action="needs_more_evidence",
                reason_code="llm_judge",
                requested_purpose="support",
            )
        ],
        "evidence_round": 1,
        "max_evidence_rounds": 2,
    }

    assert G._route_after_council_judge(state) == "evidence_planner"


def test_graph_wires_planner_before_agent_and_on_followup():
    graph = G.build_review_graph(enable_summary=False, llm=None)
    drawable = graph.get_graph()
    pairs = {(edge.source, edge.target) for edge in drawable.edges}

    assert ("council_coordinator", "evidence_planner") in pairs
    assert ("evidence_planner", "evidence_agent") in pairs
    assert ("evidence_agent", "council_judge") in pairs
    assert "evidence_planner" in drawable.nodes


def test_main_llm_is_effective_fallback_for_planner_agent_and_judge(monkeypatch):
    main_llm = object()
    captured = {}

    def planner_factory(llm):
        captured["planner"] = llm
        return lambda state: {}

    def agent_factory(tool_client=None, judge_llm=None):
        captured["agent"] = judge_llm
        return lambda state: {}

    def judge_factory(llm, judge_llm=None):
        captured["judge"] = judge_llm
        return lambda state: {}

    monkeypatch.setattr(G, "_evidence_planner_node", planner_factory)
    monkeypatch.setattr(G, "_evidence_agent_node", agent_factory)
    monkeypatch.setattr(G, "_council_judge_node", judge_factory)

    G.build_review_graph(enable_summary=False, llm=main_llm, fp_verify_llm=None)

    assert captured == {"planner": main_llm, "agent": main_llm, "judge": main_llm}


def test_graph_stats_count_only_actual_evidence_tool_calls_after_cache_reuse():
    first, task = _candidate_task()
    second = first.model_copy(update={"id": "candidate-2"})
    strategy = STRATEGIES_BY_ID["general_review.counter"]

    def request(candidate: CandidateIssue) -> EvidenceRequest:
        dossier = G._assemble_state_dossiers(
            {
                "candidate_issues": [candidate],
                "review_tasks": [task],
                "risk_profiles": {},
                "task_context_bundles": {},
                "evidence_requests": [],
                "evidence_notes": [],
                "council_verdicts": [],
            }
        ).dossiers[0]
        calls = strategy.build_tool_calls(dossier)
        return EvidenceRequest(
            candidate_id=candidate.id,
            strategy_id=strategy.id,
            purpose=strategy.purpose,
            target=task.file,
            question=strategy.question_template,
            preferred_tools=list(dict.fromkeys(call.tool_name for call in calls)),
        )

    class Client:
        calls = 0

        def get_file_content(self, file_path: str) -> ToolResponse:
            assert file_path == task.file
            self.calls += 1
            return ToolResponse(success=True, result="class Service { void changed() {} }")

    requests = [request(first), request(second)]
    state = {
        "candidate_issues": [first, second],
        "review_tasks": [task],
        "risk_profiles": {},
        "task_context_bundles": {},
        "evidence_requests": requests,
        "evidence_notes": [],
        "council_verdicts": [],
        "evidence_round": 0,
        "structured_method": "function_calling",
    }
    client = Client()

    evidence_out = G._evidence_agent_node(tool_client=client, judge_llm=None)(state)
    judge_state = {
        **state,
        "evidence_notes": evidence_out["evidence_notes"],
        "evidence_round": evidence_out["evidence_round"],
        "council_trace": evidence_out["council_trace"],
    }
    judge_out = G._council_judge_node(None)(judge_state)
    stats = judge_out["council_stats"]

    assert client.calls == 1
    assert sum(
        trace.event == "evidence_tool_reused"
        for trace in evidence_out["council_trace"]
    ) == 1
    assert stats.actual_evidence_tool_calls == 1
    assert stats.average_evidence_tool_calls == 0.5
