"""单个变更声明组的有界 React 执行器。

执行器接收一个行为调查目标和少量已解析导航入口，不接收预构造候选。
工具白名单、轮数和调用数在运行时绑定，原对话预留一次结果提交机会。
它只返回 InvestigationResult，候选绑定与最终裁决仍由管线完成。
"""

from __future__ import annotations
import logging
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from collections.abc import Callable, Mapping
from contextvars import copy_context
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any
from codeguard_agent.models.tasks import InvestigationResult, SubtaskInstruction
from codeguard_agent.pipeline.controlled.llm_contracts import LlmInvestigationResult
from codeguard_agent.pipeline.execution.discovery import (
    COMPLETE_PATCH_RESULT,
    REPEATED_TOOL_RESULT,
    SUBTASK_BUDGET_TERMINAL_RESULT,
    SUBTASK_NO_PROGRESS_TERMINAL_RESULT,
    DiscoveryToolRecord,
)
from codeguard_agent.models.evidence import EvidenceValidationStatus
from codeguard_agent.pipeline.evidence.graph_response import validate_graph_payload
from codeguard_agent.pipeline.prompting import render_prompt_template

logger = logging.getLogger("codeguard")
_PROMPT = (
    Path(__file__).resolve().parents[2] / "prompts" / "controlled" / "change-review.txt"
)


@dataclass
class SubtaskReactOutcome:
    result: InvestigationResult | None
    status: str
    reason: str = ""
    records: list[DiscoveryToolRecord] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


class SubtaskReactEngine:
    """运行范围封闭的调查 React，终止时最多做一次无工具结构化收口。"""

    def __init__(
        self,
        tool_client: Any,
        *,
        max_tool_calls: int = 10,
        max_rounds: int = 6,
        timeout_seconds: int = 120,
        initial_context: str = "",
        system_prompt: str = "",
        prepare_context: Callable[[], str] | None = None,
    ) -> None:
        self._tool_client = tool_client
        self._initial_context = initial_context
        self._prepare_context = prepare_context
        self._system_prompt = system_prompt or _PROMPT.read_text(encoding="utf-8")
        self._max_tool_calls = max(0, max_tool_calls)
        self._max_rounds = max(1, max_rounds)
        self._timeout_seconds = max(1, timeout_seconds)
        self._round_limit_hit = False
        self._cancelled = Event()
        self._deadline: float | None = None
        self._context_conclusion_used = False
        self._tool_limit_hit = False

    def run(
        self,
        llm: Any,
        *,
        task: Any,
        symbol_context: Any,
        instruction: SubtaskInstruction,
        scoped_context: Mapping[str, Any] | None = None,
        structured_method: str,
        max_retries: int,
    ) -> SubtaskReactOutcome:
        return self._run(
            llm,
            task=task,
            symbol_context=symbol_context,
            instruction=instruction,
            scoped_context=scoped_context,
            structured_method=structured_method,
            max_retries=max_retries,
        )

    def _run(
        self,
        llm: Any,
        *,
        task: Any,
        symbol_context: Any,
        instruction: SubtaskInstruction,
        scoped_context: Mapping[str, Any] | None,
        structured_method: str,
        max_retries: int,
    ) -> SubtaskReactOutcome:
        before = 0
        before_tool_calls = 0
        if llm is None:
            return SubtaskReactOutcome(None, "failed", "llm_unavailable")
        self._deadline = monotonic() + self._timeout_seconds
        self._cancelled.clear()

        def run_prepared_agent():
            if self._prepare_context is not None:
                self._initial_context = self._prepare_context()
            if self._cancelled.is_set() or monotonic() >= self._deadline:
                raise TimeoutError("subtask_timeout")
            return self._run_agent(
                llm,
                self._build_user_prompt(
                    task, symbol_context, instruction, scoped_context=scoped_context
                ),
                instruction,
                structured_method,
            )

        try:
            executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="subtask-react"
            )
            future = executor.submit(copy_context().run, run_prepared_agent)
            try:
                raw = future.result(timeout=self._timeout_seconds)
            except TimeoutError:
                self._cancelled.set()
                future.cancel()
                close = getattr(self._tool_client, "close", None)
                if callable(close):
                    close()
                records = list(getattr(self._tool_client, "trace_records", ()))[before:]
                executor.shutdown(wait=False, cancel_futures=True)
                return SubtaskReactOutcome(
                    None, "failed", "subtask_timeout", records, ["subtask_timeout"]
                )
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        except Exception as exc:
            records = list(getattr(self._tool_client, "trace_records", ()))[before:]
            if isinstance(exc, TimeoutError):
                return SubtaskReactOutcome(
                    None, "failed", "subtask_timeout", records, ["subtask_timeout"]
                )
            error_name = type(exc).__name__
            inconclusive = error_name in {
                "GraphRecursionError",
                "GraphRecursionLimitError",
            }
            budget_hit = bool(getattr(self._tool_client, "budget_exhausted", False))
            no_progress_hit = bool(
                getattr(self._tool_client, "no_progress_exhausted", False)
            )
            terminal_hit = budget_hit or no_progress_hit
            return SubtaskReactOutcome(
                None,
                "inconclusive" if inconclusive else "failed",
                "no_progress_detected"
                if inconclusive and no_progress_hit
                else "tool_budget_exceeded"
                if inconclusive and budget_hit
                else "subtask_recursion_limit"
                if inconclusive
                else error_name,
                records,
                [
                    "subtask_no_progress_terminated"
                    if inconclusive and no_progress_hit
                    else "subtask_tool_budget_exceeded"
                    if inconclusive and budget_hit
                    else "subtask_inconclusive"
                    if inconclusive
                    else "subtask_react_failed"
                ],
            )
        records = list(getattr(self._tool_client, "trace_records", ()))[before:]
        actual_tool_calls = (
            int(getattr(self._tool_client, "tool_calls", len(records)) or 0)
            - before_tool_calls
        )
        if actual_tool_calls > self._max_tool_calls:
            return SubtaskReactOutcome(
                None,
                "inconclusive",
                "tool_budget_exceeded",
                records,
                ["subtask_tool_budget_exceeded"],
            )
        parsed = self._extract(raw)
        if parsed is not None:
            contract_error = self._terminal_error(parsed)
            if contract_error:
                return SubtaskReactOutcome(
                    None, "failed", contract_error, records, ["subtask_protocol_failed"]
                )
        budget_hit = (
            bool(getattr(self._tool_client, "budget_exhausted", False))
            or self._tool_limit_hit
        )
        no_progress_hit = bool(
            getattr(self._tool_client, "no_progress_exhausted", False)
        )
        terminal_hit = budget_hit or no_progress_hit or self._round_limit_hit
        terminal_reason = (
            "no_progress_detected"
            if no_progress_hit
            else "tool_budget_exceeded"
            if budget_hit
            else "subtask_round_limit"
        )
        if self._context_conclusion_used and parsed is None:
            return SubtaskReactOutcome(
                None,
                "failed",
                "context_conclusion_missing_or_invalid",
                records,
                ["subtask_protocol_failed"],
            )
        if self._context_conclusion_used and parsed is not None:
            if parsed.outcome == "no_finding" and any(
                (self._record_is_usable_source(record) for record in records)
            ):
                parsed = parsed.model_copy(
                    update={"subtask_id": instruction.subtask_id}
                )
                return SubtaskReactOutcome(
                    parsed,
                    "complete",
                    f"{terminal_reason}_context_conclusion",
                    records,
                    ["subtask_react_context_conclusion"],
                )
            if parsed.outcome == "failed":
                return SubtaskReactOutcome(
                    parsed,
                    "failed",
                    "context_conclusion_failed",
                    records,
                    ["subtask_react_failed"],
                )
        if terminal_hit and parsed is not None and (parsed.outcome == "findings"):
            if parsed.subtask_id != instruction.subtask_id:
                parsed = parsed.model_copy(
                    update={"subtask_id": instruction.subtask_id}
                )
            return SubtaskReactOutcome(
                parsed,
                "complete",
                f"{terminal_reason}_context_conclusion"
                if self._context_conclusion_used
                else "no_progress_after_findings"
                if no_progress_hit
                else f"{terminal_reason}_after_findings",
                records,
                [
                    "subtask_react_context_conclusion"
                    if self._context_conclusion_used
                    else "subtask_react_findings_after_no_progress"
                    if no_progress_hit
                    else "subtask_react_findings_after_budget"
                ],
            )
        if terminal_hit:
            return SubtaskReactOutcome(
                parsed
                if parsed is not None and parsed.outcome == "inconclusive"
                else None,
                "inconclusive",
                f"{terminal_reason}_context_conclusion"
                if self._context_conclusion_used
                else terminal_reason,
                records,
                [
                    "subtask_react_context_conclusion"
                    if self._context_conclusion_used
                    else "subtask_no_progress_terminated"
                    if no_progress_hit
                    else "subtask_tool_budget_exceeded"
                    if budget_hit
                    else "subtask_round_limit"
                ],
            )
        if parsed is None:
            return SubtaskReactOutcome(
                None,
                "failed",
                "investigation_result_missing_or_invalid",
                records,
                ["subtask_protocol_failed"],
            )
        if parsed.subtask_id != instruction.subtask_id:
            parsed = parsed.model_copy(update={"subtask_id": instruction.subtask_id})
        return SubtaskReactOutcome(
            parsed,
            parsed.outcome
            if parsed.outcome in {"inconclusive", "failed"}
            else "complete",
            records=records,
            events=[f"subtask_react_{parsed.outcome}"],
        )

    @staticmethod
    def _record_is_usable_source(record: DiscoveryToolRecord) -> bool:
        """Return whether a record contains source that can support a negative.

        A relationship or empty page alone cannot refute a claim. Accept real
        source reads and complete source excerpts embedded in a validated graph
        page, without requiring a redundant call to a particular tool name.
        """
        if str(getattr(record, "tool", "")) == "query_relations":
            if not SubtaskReactEngine._record_contains_fact(record):
                return False
            payload = json.loads(record.resolved_output or record.output)
            return any(
                (
                    isinstance(symbol.get("source_excerpt"), dict)
                    and symbol["source_excerpt"].get("truncated") is False
                    and bool(symbol["source_excerpt"].get("text"))
                    for symbol in payload.get("symbols", ())
                    if isinstance(symbol, dict)
                )
            )
        if str(getattr(record, "tool", "")) not in {"read_symbol"}:
            return False
        if not SubtaskReactEngine._record_contains_fact(record):
            return False
        raw = str(
            getattr(record, "resolved_output", "")
            or getattr(record, "output", "")
            or ""
        ).strip()
        return (
            raw
            not in {
                COMPLETE_PATCH_RESULT,
                REPEATED_TOOL_RESULT,
                SUBTASK_BUDGET_TERMINAL_RESULT,
                SUBTASK_NO_PROGRESS_TERMINAL_RESULT,
            }
            and "end_of_symbol: true" not in raw
        )

    @staticmethod
    def _record_contains_fact(record: DiscoveryToolRecord) -> bool:
        """Return whether a successful tool record contains usable facts.

        A non-empty serialized value is not enough: ``{}``, ``null`` and
        malformed graph payloads are execution artifacts, not observations
        that can support a negative conclusion.  Source reads are accepted
        when they contain text; graph reads must carry the v2 contract and at
        least one resolved symbol or relationship.
        """
        if str(getattr(record, "status", "")) not in {
            "complete",
            "reused",
            "available",
        }:
            return False
        raw = str(
            getattr(record, "resolved_output", "")
            or getattr(record, "output", "")
            or ""
        ).strip()
        if not raw or raw in {
            COMPLETE_PATCH_RESULT,
            REPEATED_TOOL_RESULT,
            SUBTASK_BUDGET_TERMINAL_RESULT,
            SUBTASK_NO_PROGRESS_TERMINAL_RESULT,
        }:
            return False
        tool = str(getattr(record, "tool", ""))
        if tool not in {"query_relations"}:
            return raw not in {"{}", "null", "[]"}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        arguments = getattr(record, "arguments", {})
        expected_subject = ""
        if isinstance(arguments, dict):
            expected_subject = str(
                arguments.get("subject_symbol_id") or arguments.get("symbol_id") or ""
            )
        validation = validate_graph_payload(
            raw, tool=tool, expected_subject=expected_subject
        )
        if validation.status is not EvidenceValidationStatus.VALID:
            return False
        payload.get("symbols")
        relationships = payload.get("relationships")
        return bool(isinstance(relationships, list) and relationships or False)

    def _run_agent(
        self, llm: Any, user_prompt: str, instruction: SubtaskInstruction, method: str
    ) -> Any:
        from langchain.agents import create_agent
        from langchain.agents.middleware import (
            after_model,
            before_model,
            wrap_model_call,
        )
        from langchain.agents.middleware.types import ModelResponse
        from langchain.agents.structured_output import ToolStrategy
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.utils.function_calling import convert_to_openai_tool
        from pydantic import ValidationError
        from codeguard_agent.tools.definitions import (
            make_query_relations_tool,
            make_read_symbol_tool,
        )
        from codeguard_agent.pipeline.controlled.investigation_tools import (
            require_fact_question,
        )
        from codeguard_agent.pipeline.controlled.investigation_decision import (
            decision_error,
            decision_messages,
            decision_schema,
        )

        factories = {
            "read_symbol": lambda: make_read_symbol_tool(self._tool_client),
            "query_relations": lambda: make_query_relations_tool(self._tool_client),
        }
        tools = [
            require_fact_question(factories[name]())
            for name in instruction.allowed_tools
            if name in factories
        ]
        step_schema = decision_schema(tools)
        step_tool = convert_to_openai_tool(step_schema)
        consumed_observations: set[str] = set()
        decision_rejected = False
        original_decisions: dict[str, Any] = {}
        feedback_prompt = _PROMPT.with_name(
            "investigation-decision-feedback.txt"
        ).read_text(encoding="utf-8")
        model_calls = 0
        self._round_limit_hit = False
        self._tool_limit_hit = False
        self._context_conclusion_used = False
        initial_calls = int(getattr(self._tool_client, "tool_calls", 0) or 0)
        budget_prompt = _PROMPT.with_name("investigation-budget.txt").read_text(
            encoding="utf-8"
        )

        def remaining_tools() -> int:
            spent = (
                int(getattr(self._tool_client, "tool_calls", 0) or 0) - initial_calls
            )
            return max(0, self._max_tool_calls - spent)

        @before_model(can_jump_to=["end"])
        def stop_closed_investigation(
            state: Any, runtime: Any
        ) -> dict[str, Any] | None:
            nonlocal model_calls
            if self._cancelled.is_set() or (
                self._deadline is not None and monotonic() >= self._deadline
            ):
                raise TimeoutError("subtask deadline reached")
            if self._context_conclusion_used:
                return {"jump_to": "end"}
            closed = getattr(self._tool_client, "budget_exhausted", False) or getattr(
                self._tool_client, "no_progress_exhausted", False
            )
            self._tool_limit_hit = remaining_tools() == 0
            self._round_limit_hit = model_calls >= self._max_rounds
            if closed or self._tool_limit_hit or self._round_limit_hit:
                self._context_conclusion_used = True
                close = getattr(self._tool_client, "close", None)
                if callable(close):
                    close()
            else:
                model_calls += 1
            return None

        @after_model(can_jump_to=["model"])
        def retry_rejected_decision(state: Any, runtime: Any) -> dict[str, Any] | None:
            if decision_rejected:
                return {"jump_to": "model"}
            return None

        @wrap_model_call
        def investigation_budget(request: Any, handler: Any) -> Any:
            nonlocal consumed_observations, decision_rejected
            decision_rejected = False
            known = set(getattr(self._tool_client, "observation_aliases", {}))
            pending = known - consumed_observations
            phase = "conclude" if self._context_conclusion_used else "explore"
            notice = render_prompt_template(
                budget_prompt,
                {
                    "phase": phase,
                    "phase_instruction": _PROMPT.with_name(
                        f"investigation-{phase}.txt"
                    ).read_text(encoding="utf-8"),
                    "rounds_left": str(
                        0
                        if self._context_conclusion_used
                        else self._max_rounds - model_calls + 1
                    ),
                    "tools_left": str(
                        0 if self._context_conclusion_used else remaining_tools()
                    ),
                    "pending_observations": ",".join(sorted(pending)) or "none",
                },
            )
            messages = [
                *decision_messages(request.messages, original_decisions),
                HumanMessage(content=notice),
            ]
            if not self._context_conclusion_used:
                model = request.model.bind_tools(
                    [step_tool],
                    tool_choice={
                        "type": "function",
                        "function": {"name": "LlmInvestigationDecision"},
                    },
                    **request.model_settings,
                )
                output = model.invoke(
                    ([request.system_message] if request.system_message else [])
                    + messages
                )
                calls: Any = getattr(output, "tool_calls", ())
                decision = None
                error = "expected_one_investigation_decision"
                if (
                    len(calls) == 1
                    and calls[0].get("name") == "LlmInvestigationDecision"
                ):
                    try:
                        decision = step_schema.model_validate(calls[0].get("args"))
                        error = decision_error(
                            decision,
                            aliases=getattr(self._tool_client, "symbol_aliases", {}),
                            known=known,
                            pending=pending,
                        )
                        if not error and decision.result is not None:
                            internal_result = self._extract(
                                {"structured_response": decision.result}
                            )
                            error = (
                                self._terminal_error(internal_result)
                                if internal_result is not None
                                else "invalid_result_contract"
                            )
                    except ValidationError as exc:
                        details = [
                            {
                                "path": ".".join((str(part) for part in item["loc"])),
                                "error": item["type"],
                                "message": item["msg"],
                            }
                            for item in exc.errors(
                                include_input=False, include_url=False
                            )[:4]
                        ]
                        error = (
                            "invalid_decision_schema:"
                            + json.dumps(details, ensure_ascii=False)[:1400]
                        )
                    except (TypeError, ValueError):
                        error = "invalid_decision_schema"
                if error or decision is None:
                    decision_rejected = True
                    content = json.dumps(calls, ensure_ascii=False)[:2500]
                    feedback = render_prompt_template(
                        feedback_prompt, {"reason": error}
                    )
                    return ModelResponse(
                        result=[
                            AIMessage(content=content),
                            HumanMessage(content=feedback),
                        ]
                    )
                consumed_observations = known
                if decision.result is not None:
                    return ModelResponse(
                        result=[AIMessage(content=decision.model_dump_json())],
                        structured_response=decision.result,
                    )
                checkpoint = decision.model_dump_json(exclude={"queries", "result"})
                query_calls = [
                    {
                        "name": query.tool,
                        "args": query.arguments.model_dump(exclude_none=True),
                        "id": f"{calls[0]['id']}-q{index + 1}",
                        "type": "tool_call",
                    }
                    for index, query in enumerate(decision.queries)
                ]
                for query_call in query_calls:
                    original_decisions[query_call["id"]] = {
                        **calls[0],
                        "args": decision.model_dump(exclude_none=True),
                    }
                return ModelResponse(
                    result=[AIMessage(content=checkpoint, tool_calls=query_calls)]
                )
            model = request.model.bind_tools(
                [step_tool],
                tool_choice={
                    "type": "function",
                    "function": {"name": "LlmInvestigationDecision"},
                },
                **request.model_settings,
            )
            output = model.invoke(
                ([request.system_message] if request.system_message else []) + messages
            )
            calls = getattr(output, "tool_calls", ())
            parsed = None
            if len(calls) == 1 and calls[0].get("name") == "LlmInvestigationResult":
                parsed = self._extract({"structured_response": calls[0].get("args")})
            elif len(calls) == 1 and calls[0].get("name") == "LlmInvestigationDecision":
                arguments = calls[0].get("args")
                if isinstance(arguments, dict) and arguments.get("queries") in (
                    None,
                    [],
                    (),
                ):
                    parsed = self._extract(
                        {"structured_response": arguments.get("result")}
                    )
            if parsed is None:
                return ModelResponse(
                    result=[AIMessage(content="context_conclusion_missing_or_invalid")]
                )
            return ModelResponse(result=[output], structured_response=parsed)

        agent: Any = create_agent(
            llm,
            tools,
            system_prompt=self._system_prompt,
            response_format=ToolStrategy(LlmInvestigationResult, handle_errors=True),
            middleware=[
                stop_closed_investigation,
                investigation_budget,
                retry_rejected_decision,
            ],
        )
        return agent.invoke(
            {"messages": [("human", user_prompt)]},
            config={"recursion_limit": max(12, self._max_rounds * 6 + 6)},
        )

    @staticmethod
    def _extract(raw: Any) -> InvestigationResult | None:
        if not isinstance(raw, dict):
            return None
        value = raw.get("structured_response")
        if value is None:
            return None
        try:
            provider = LlmInvestigationResult.model_validate(
                value.model_dump() if hasattr(value, "model_dump") else value
            )
            return InvestigationResult.model_validate(provider.model_dump())
        except Exception:
            return None

    def _terminal_error(self, result: InvestigationResult) -> str:
        error = self._result_contract_error(result, allow_patch_only=True)
        if error:
            return error
        known = set(getattr(self._tool_client, "observation_aliases", {}))
        referenced = {
            ref.observation_id
            for finding in result.findings
            for ref in finding.observations
        }
        if referenced - known:
            return "unknown_finding_observations:" + ",".join(
                sorted(referenced - known)
            )
        return ""

    @staticmethod
    def _result_contract_error(
        result: InvestigationResult, *, allow_patch_only: bool = False
    ) -> str:
        """Reject contradictory terminal payloads before they enter State.

        Pydantic validates field shapes, but the cross-field meaning is part of
        the React protocol: ``findings`` must contain evidence-bearing entries,
        and non-finding outcomes must not smuggle findings that the coordinator
        would silently ignore.  Failing here keeps the state machine explicit
        and makes malformed provider output visible in Trace.
        """
        if result.outcome == "findings":
            if not result.findings:
                return "findings_outcome_without_findings"
            if not allow_patch_only and any(
                (not finding.observations for finding in result.findings)
            ):
                return "finding_without_observations"
            return ""
        if result.findings:
            return "findings_present_on_non_findings_outcome"
        return ""

    def _build_user_prompt(
        self,
        task: Any,
        symbol_context: Any,
        instruction: SubtaskInstruction,
        *,
        scoped_context: dict[str, Any] | None = None,
    ) -> str:
        from codeguard_agent.pipeline.controlled.investigation_decision import (
            symbol_name,
        )

        initial_ids = set(instruction.initial_symbol_ids)
        initial_symbols = [
            {"symbol_id": raw, "name": symbol_name(raw)}
            for raw in instruction.initial_symbol_ids
        ]
        symbols = [
            s.model_dump()
            for s in getattr(symbol_context, "symbols", ())
            if s.symbol_id in initial_ids
        ]
        references = [
            r.model_dump()
            for r in getattr(symbol_context, "references", ())
            if r.symbol_id in initial_ids
        ]
        scoped = scoped_context if scoped_context is not None else {}
        patch = str(scoped.get("patch", getattr(task, "patch", "")))
        raw_anchors: Any = scoped.get("deletion_anchors")
        if raw_anchors is None:
            raw_anchors = getattr(task, "deletion_anchors", ())
        anchors = [
            item.model_dump() if hasattr(item, "model_dump") else item
            for item in raw_anchors
        ]
        other_changes = scoped.get("other_changes_index", ())
        primary_lines = scoped.get("primary_change_lines", ())
        scope_kind = str(scoped.get("scope_kind", "resolved"))
        scope_note = (
            "本组范围已由运行时隔离；其它变更只作为索引，不是本轮候选目标。"
            if scoped_context is not None
            else "本组没有额外的隔离视图；以运行时提供的 task_patch 为准。"
        )
        return f'''<subtask id="{instruction.subtask_id}">\n<scope>{scope_note}</scope>\n<scope_kind>{scope_kind}</scope_kind>\n<scoped_task_patch file="{task.file}">\n{patch}\n</scoped_task_patch>\n<primary_change_lines>{json.dumps(list(primary_lines), ensure_ascii=False, separators=(",", ":"))}</primary_change_lines>\n<deletion_anchors>{json.dumps(anchors, ensure_ascii=False, separators=(",", ":"))}</deletion_anchors>\n<other_changes_index>{json.dumps(list(other_changes), ensure_ascii=False, separators=(",", ":"))}</other_changes_index>\n<initial_symbols>{json.dumps(initial_symbols, separators=(",", ":"))}</initial_symbols>\n<symbol_context>{json.dumps(symbols, ensure_ascii=False)}</symbol_context>\n<changed_references>{json.dumps(references, ensure_ascii=False)}</changed_references>\n<prepared_source>{self._initial_context}</prepared_source>\n<instruction>{instruction.model_dump_json(exclude_defaults=True)}</instruction>\n'''


__all__ = ["SubtaskReactEngine", "SubtaskReactOutcome"]
