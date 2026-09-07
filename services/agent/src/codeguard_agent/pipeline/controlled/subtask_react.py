"""单个 GraphPlan 子任务的有界 React 执行器。

这个执行器和历史 reviewer ReAct 有意不同：它不接收知识库或预构造候选，
只接收一个调查目标；工具白名单、起始 symbol、轮数和调用数在运行时绑定。
它只返回 InvestigationResult，候选绑定与最终裁决仍由管线完成。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codeguard_agent.models.tasks import InvestigationResult, SubtaskInstruction
from codeguard_agent.pipeline.controlled.llm_contracts import LlmInvestigationResult
from codeguard_agent.pipeline.execution.discovery import DiscoveryToolRecord

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
    """运行一个范围封闭的调查 React，不做 synthesis、不启动 replan。"""

    def __init__(
        self,
        tool_client: Any,
        *,
        max_tool_calls: int = 4,
        max_rounds: int = 4,
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
            return SubtaskReactOutcome(
                None,
                "inconclusive" if inconclusive else "failed",
                (
                    "tool_budget_exceeded"
                    if inconclusive and budget_hit
                    else "subtask_recursion_limit"
                    if inconclusive
                    else error_name
                ),
                records,
                [
                    "subtask_tool_budget_exceeded"
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
        # The coordinator records rejected calls without incrementing its
        # successful-call counter.  Inspect the explicit flag as well, or a
        # model could turn a rejected final query into a false ``no_finding``.
        if bool(getattr(self._tool_client, "budget_exhausted", False)):
            return SubtaskReactOutcome(
                None,
                "inconclusive",
                "tool_budget_exceeded",
                records,
                ["subtask_tool_budget_exceeded"],
            )
        parsed = self._extract(raw)
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

    def _run_agent(self, llm: Any, user_prompt: str, instruction: SubtaskInstruction, method: str) -> Any:
        from langchain.agents import create_agent
        from langchain.agents.structured_output import ToolStrategy
        from codeguard_agent.tools.definitions import (
            make_change_impact_tool,
            make_file_content_tool,
            make_path_tool,
            make_structure_tool,
        )

        factories = {
            "get_file_content": lambda: make_file_content_tool(self._tool_client),
            "inspect_change_impact": lambda: make_change_impact_tool(self._tool_client),
            "inspect_path": lambda: make_path_tool(self._tool_client),
            "inspect_structure": lambda: make_structure_tool(self._tool_client),
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
            f"<instruction>\n{safe_instruction.model_dump_json(exclude_defaults=True)}\n</instruction>\n"
            "只调查该子任务；根据实际工具事实返回 InvestigationResult。"
        )


__all__ = ["SubtaskReactEngine", "SubtaskReactOutcome"]
