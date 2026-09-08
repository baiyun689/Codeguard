"""单个 GraphPlan 子任务的有界 React 执行器。

这个执行器和历史 reviewer ReAct 有意不同：它不接收知识库或预构造候选，
只接收一个调查目标；工具白名单、起始 symbol、轮数和调用数在运行时绑定。
它只返回 InvestigationResult，候选绑定与最终裁决仍由管线完成。
"""

from __future__ import annotations

import logging
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codeguard_agent.models.tasks import InvestigationResult, SubtaskInstruction
from codeguard_agent.pipeline.controlled.llm_contracts import LlmInvestigationResult
from codeguard_agent.pipeline.execution.discovery import DiscoveryToolRecord
from codeguard_agent.llm.client import invoke_with_retry

logger = logging.getLogger("codeguard")
_PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "controlled" / "execute-subtask-react.txt"


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
        max_tool_calls: int = 20,
        max_rounds: int = 12,
        timeout_seconds: int = 120,
    ) -> None:
        self._tool_client = tool_client
        self._max_tool_calls = max(0, max_tool_calls)
        self._max_rounds = max(1, max_rounds)
        self._timeout_seconds = max(1, timeout_seconds)

    def run(
        self,
        llm: Any,
        *,
        task: Any,
        symbol_context: Any,
        instruction: SubtaskInstruction,
        structured_method: str,
        max_retries: int,
    ) -> SubtaskReactOutcome:
        before = len(getattr(self._tool_client, "trace_records", ()))
        before_tool_calls = int(getattr(self._tool_client, "tool_calls", 0) or 0)
        if llm is None:
            return SubtaskReactOutcome(None, "failed", "llm_unavailable")
        try:
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subtask-react")
            future = executor.submit(
                self._run_agent,
                llm,
                self._build_user_prompt(task, symbol_context, instruction),
                instruction,
                structured_method,
            )
            try:
                raw = future.result(timeout=self._timeout_seconds)
            except TimeoutError:
                future.cancel()
                close = getattr(self._tool_client, "close", None)
                if callable(close):
                    close()
                records = list(getattr(self._tool_client, "trace_records", ()))[before:]
                executor.shutdown(wait=False, cancel_futures=True)
                return SubtaskReactOutcome(
                    None,
                    "inconclusive",
                    "subtask_timeout",
                    records,
                    ["subtask_timeout"],
                )
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        except Exception as exc:  # noqa: BLE001 - one subtask cannot abort its task
            records = list(getattr(self._tool_client, "trace_records", ()))[before:]
            # LangGraph raises GraphRecursionError when the provider keeps
            # issuing tool turns after the bounded investigation has already
            # reached its useful frontier.  That is an inconclusive evidence
            # result, not a broken task; the caller must keep the gap visible
            # without counting it as a hard execution failure.
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
            if inconclusive and terminal_hit:
                finalized = self._finalize_after_budget(
                    llm,
                    task=task,
                    symbol_context=symbol_context,
                    instruction=instruction,
                    records=records,
                    structured_method=structured_method,
                    max_retries=max_retries,
                    termination_reason=(
                        "no_progress" if no_progress_hit else "tool_budget_exceeded"
                    ),
                )
                if finalized is not None and finalized.outcome == "findings":
                    return SubtaskReactOutcome(
                        finalized,
                        "complete",
                        (
                            "no_progress_finalized"
                            if no_progress_hit
                            else "tool_budget_exceeded_finalized"
                        ),
                        records,
                        [
                            "subtask_react_findings_after_no_progress"
                            if no_progress_hit
                            else "subtask_react_findings_after_budget"
                        ],
                    )
            return SubtaskReactOutcome(
                None,
                "inconclusive" if inconclusive else "failed",
                (
                    "no_progress_detected"
                    if inconclusive and no_progress_hit
                    else
                    "tool_budget_exceeded"
                    if inconclusive and budget_hit
                    else "subtask_recursion_limit"
                    if inconclusive
                    else error_name
                ),
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
        actual_tool_calls = int(getattr(
            self._tool_client,
            "tool_calls",
            len(records),
        ) or 0) - before_tool_calls
        if actual_tool_calls > self._max_tool_calls:
            return SubtaskReactOutcome(
                None,
                "inconclusive",
                "tool_budget_exceeded",
                records,
                ["subtask_tool_budget_exceeded"],
            )
        parsed = self._extract(raw)
        # A provider may emit the terminal structured result immediately after
        # the first rejected call.  Preserve a finding backed by the successful
        # observations already captured; only ``no_finding`` remains unsafe
        # after a terminal rejection because the model may not have completed
        # its negative search.
        budget_hit = bool(getattr(self._tool_client, "budget_exhausted", False))
        no_progress_hit = bool(
            getattr(self._tool_client, "no_progress_exhausted", False)
        )
        terminal_hit = budget_hit or no_progress_hit
        if terminal_hit and parsed is not None and parsed.outcome == "findings":
            if parsed.subtask_id != instruction.subtask_id:
                parsed = parsed.model_copy(update={"subtask_id": instruction.subtask_id})
            return SubtaskReactOutcome(
                parsed,
                "complete",
                (
                    "no_progress_after_findings"
                    if no_progress_hit
                    else "tool_budget_exceeded_after_findings"
                ),
                records,
                [
                    "subtask_react_findings_after_no_progress"
                    if no_progress_hit
                    else "subtask_react_findings_after_budget"
                ],
            )
        # The coordinator records rejected calls without incrementing its
        # successful-call counter.  Do not let a rejected final query become a
        # false ``no_finding`` or a normal completed result.
        if terminal_hit:
            return SubtaskReactOutcome(
                None,
                "inconclusive",
                "no_progress_detected" if no_progress_hit else "tool_budget_exceeded",
                records,
                [
                    "subtask_no_progress_terminated"
                    if no_progress_hit
                    else "subtask_tool_budget_exceeded"
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
            "complete",
            records=records,
            events=[f"subtask_react_{parsed.outcome}"],
        )

    def _finalize_after_budget(
        self,
        llm: Any,
        *,
        task: Any,
        symbol_context: Any,
        instruction: SubtaskInstruction,
        records: list[DiscoveryToolRecord],
        structured_method: str,
        max_retries: int,
        termination_reason: str = "tool_budget_exceeded",
    ) -> InvestigationResult | None:
        """Close a terminal React using captured facts only.

        Some providers keep emitting tool calls after the gate has rejected a
        call, which makes LangGraph raise before its structured terminal turn.
        A single no-tool structured call is a protocol finalizer, not another
        investigation: it receives only the bounded tool outputs already
        captured by this subtask and may emit findings only with their local
        observation IDs.  Negative results are deliberately not accepted here.
        """

        observations: list[str] = []
        for record in records:
            status = str(getattr(record, "status", ""))
            if status not in {"complete", "reused", "available"}:
                continue
            # ``output`` for a reused call is intentionally only a short
            # marker.  The coordinator keeps the first real payload in
            # ``resolved_output``; use that payload so a finalizer never
            # reasons from a cache marker.  The call id is the stable bridge
            # that the executor later maps to the ledger's Txx alias.
            output = str(
                getattr(record, "resolved_output", "")
                or getattr(record, "output", "")
                or ""
            )
            if not output:
                continue
            observations.append(
                json.dumps(
                    {
                        "tool": getattr(record, "tool", ""),
                        "observation_id": str(
                            getattr(record, "reused_from_call_id", "")
                            or getattr(record, "call_id", "")
                        ),
                        "output": output[:3500],
                    },
                    ensure_ascii=False,
                )
            )
        if not observations:
            return None
        reason_text = (
            "工具调用预算耗尽" if termination_reason == "tool_budget_exceeded"
            else "连续工具调用没有产生新事实"
        )
        user = (
            self._build_user_prompt(task, symbol_context, instruction)
            + "\n<captured_observations>\n"
            + "\n".join(observations[:8])
            + "\n</captured_observations>\n"
            f"ReAct 因{reason_text}未正常收口。你现在只能依据上面已捕获的工具输出做一次最终结构化收口；"
            "不要调用工具，不要输出 no_finding。若这些 observation 不能直接支持完整机制，"
            "返回 inconclusive；只有能绑定实际 observation_id（原样填写上面 observation_id）"
            "时才返回 findings。"
        )
        system = (
            _PROMPT.read_text(encoding="utf-8")
            + "\n\n这是终止后的无工具协议收口，不得补充任何未出现在 captured_observations 的事实。"
        )
        try:
            raw = invoke_with_retry(
                llm.with_structured_output(
                    LlmInvestigationResult,
                    method=structured_method,
                ),
                [("system", system), ("human", user)],
                max_retries=max(1, max_retries),
            )
        except Exception:  # noqa: BLE001 - finalizer is best effort
            return None
        parsed = self._extract(
            {"structured_response": raw}
            if raw is not None and not isinstance(raw, dict)
            else raw
        )
        if parsed is None:
            return None
        if parsed.subtask_id != instruction.subtask_id:
            parsed = parsed.model_copy(update={"subtask_id": instruction.subtask_id})
        return parsed

    def _run_agent(self, llm: Any, user_prompt: str, instruction: SubtaskInstruction, method: str) -> Any:
        from langchain.agents import create_agent
        from langchain.agents.structured_output import ToolStrategy
        from codeguard_agent.tools.definitions import (
            make_query_relations_tool,
            make_read_symbol_tool,
        )

        # The subtask React path deliberately has only the two canonical
        # actions.  Legacy inspect_* / get_file_content names remain available
        # to the planned_steps compatibility executor, never to this dynamic
        # investigation loop.
        factories = {
            "read_symbol": lambda: make_read_symbol_tool(self._tool_client),
            "query_relations": lambda: make_query_relations_tool(self._tool_client),
        }
        tools = [factories[name]() for name in instruction.allowed_tools if name in factories]
        agent = create_agent(
            llm,
            tools,
            system_prompt=_PROMPT.read_text(encoding="utf-8"),
            response_format=ToolStrategy(LlmInvestigationResult, handle_errors=True),
        )
        return agent.invoke(
            {"messages": [("human", user_prompt)]},
            # One React round is a model decision plus at most one tool
            # result.  Keep the graph recursion limit close to the declared
            # round budget; the client-side tool gate remains the hard cost
            # limit for repeated or parallel tool requests.
            # A LangGraph round includes the model decision, the tool node,
            # and the model's structured-result turn.  The old ``2*n+2``
            # allowance could exhaust before the terminal InvestigationResult
            # was emitted even when the declared tool/round budget was not
            # exceeded.  Keep the runtime tool gate as the hard cost bound,
            # but leave enough graph steps for every bounded round to close.
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
        except Exception:  # noqa: BLE001
            return None

    def _build_user_prompt(self, task: Any, symbol_context: Any, instruction: SubtaskInstruction) -> str:
        aliases = getattr(self._tool_client, "symbol_aliases", {})
        raw_to_alias = {raw: alias for alias, raw in aliases.items()}

        def render_symbol(symbol: Any) -> str:
            payload = symbol.model_dump()
            raw = str(payload.get("symbol_id", ""))
            payload["symbol_id"] = raw_to_alias.get(raw, raw)
            owner = str(payload.get("owner_id", ""))
            if owner:
                payload["owner_id"] = raw_to_alias.get(owner, owner)
            import json
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        symbols = "\n".join(
            render_symbol(symbol) for symbol in (symbol_context.symbols if symbol_context else ())
            if symbol.symbol_id in set(instruction.initial_symbol_ids)
        ) or "(仅允许使用 instruction 中的 symbol_id)"
        references = "\n".join(
            json.dumps(
                {
                    **reference.model_dump(),
                    "symbol_id": raw_to_alias.get(reference.symbol_id, reference.symbol_id),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for reference in (getattr(symbol_context, "references", ()) if symbol_context else ())
        ) or "(无变更行引用目标)"
        safe_instruction = instruction.model_copy(update={
            "initial_symbol_ids": tuple(
                raw_to_alias.get(symbol_id, symbol_id)
                for symbol_id in instruction.initial_symbol_ids
            )
        })
        return (
            f'<subtask id="{instruction.subtask_id}" reviewer="{instruction.reviewer.value}" '
            f'change_unit="{instruction.change_unit_id}">\n'
            f"<task_patch file=\"{task.file}\">\n{task.patch}\n</task_patch>\n"
            f"<symbol_context>\n{symbols}\n</symbol_context>\n"
            f"<changed_references>\n{references}\n</changed_references>\n"
            f"<instruction>\n{safe_instruction.model_dump_json(exclude_defaults=True)}\n</instruction>\n"
            "只调查该子任务；根据实际工具事实返回 InvestigationResult。"
        )


__all__ = ["SubtaskReactEngine", "SubtaskReactOutcome"]
