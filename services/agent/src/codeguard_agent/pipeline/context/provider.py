"""ContextProvider：在 ReviewCouncil 前构造共享事实包。

负责解析 diff AST、规划并执行 Level-1/Level-2 上下文调用，产出 ContextBundle
供后续发现者和裁决节点共享使用。
"""

from __future__ import annotations

import json
import logging

from codeguard_agent.git.diff_collector import parse_changed_files
from codeguard_agent.models.council import ContextBundle, ContextFact
from codeguard_agent.pipeline.engines import GatheredContext
from codeguard_agent.pipeline.context.base import PipelineContext, PipelineStage
from codeguard_agent.pipeline.risk.task_prep import build_tasks

logger = logging.getLogger("codeguard")

_FACT_BUDGET = 4000


def _clip(text: str, budget: int = _FACT_BUDGET) -> tuple[str, bool]:
    if len(text) <= budget:
        return text, False
    return text[:budget] + "...(已截断)", True


def _changed_locations(diff_text: str) -> list[dict[str, object]]:
    by_file: dict[str, set[int]] = {}
    for task in build_tasks(diff_text):
        by_file.setdefault(task.file, set()).update(task.changed_lines)
    return [
        {"file": file, "lines": sorted(lines)}
        for file, lines in by_file.items()
    ]


class ContextProviderStage(PipelineStage):
    """构造 ReviewCouncil 共享 ContextBundle。

    第一版只产出事实和来源信息,不判断候选是否为真实问题。
    """

    def __init__(self, *, include_broad_scan: bool = True) -> None:
        self._include_broad_scan = include_broad_scan

    @property
    def name(self) -> str:
        return "context_provider"

    def execute(self, context: PipelineContext) -> PipelineContext:
        changed_files = parse_changed_files(context.diff_text)
        facts: list[ContextFact] = []
        diagnostics: dict[str, str] = {}

        gathered: list[GatheredContext] = []
        if context.tool_client is not None:
            changes = context.change_locations or _changed_locations(context.diff_text)
            resp = context.tool_client.resolve_change_context(changes)
            if not getattr(resp, "success", False):
                diagnostics["symbol_context"] = str(
                    getattr(resp, "error", "tool_failed") or "tool_failed"
                )
            else:
                content = (
                    resp.as_tool_output()
                    if hasattr(resp, "as_tool_output")
                    else str(resp)
                )
                try:
                    payload = json.loads(content)
                    for item in payload.get("contexts", []):
                        rendered = json.dumps(
                            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        )
                        clipped, truncated = _clip(rendered)
                        facts.append(
                            ContextFact(
                                source="tool:resolve_change_context",
                                kind="symbol_context",
                                content=clipped,
                                truncated=truncated,
                            )
                        )
                    if payload.get("status") in {"unknown", "partial"}:
                        diagnostics["symbol_context"] = "; ".join(
                            str(value) for value in payload.get("limitations", [])
                        ) or f"graph_coverage_{payload.get('status')}"
                    gathered.append(
                        GatheredContext(
                            "resolve_change_context",
                            json.dumps(changes, ensure_ascii=False),
                            content,
                        )
                    )
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    diagnostics["symbol_context"] = f"invalid_graph_response: {exc}"

        bundle = ContextBundle(
            changed_files=changed_files,
            facts=facts,
        )
        context.context_bundle = bundle
        context.context_diagnostics = diagnostics
        context.gathered_context.extend(gathered)
        fact_sources = sorted({fact.source for fact in facts} | {"diff"})
        logger.info(
            "[context_provider] %d 个文件，%d 条事实，来源=%s",
            len(changed_files),
            len(facts),
            fact_sources,
        )
        return context
