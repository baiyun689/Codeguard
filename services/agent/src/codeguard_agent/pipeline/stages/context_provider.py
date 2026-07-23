"""ADR-032 ContextProvider:在 ReviewCouncil 前构造共享事实包。"""

from __future__ import annotations

import logging
import re

from codeguard_agent.git.diff_collector import parse_changed_files
from codeguard_agent.models.council import ContextBundle, ContextFact
from codeguard_agent.pipeline.engines import GatheredContext
from codeguard_agent.pipeline.stages.base import PipelineContext, PipelineStage

logger = logging.getLogger("codeguard")

_FACT_BUDGET = 4000


def _clip(text: str, budget: int = _FACT_BUDGET) -> tuple[str, bool]:
    if len(text) <= budget:
        return text, False
    return text[:budget] + "...(已截断)", True


def _split_ast_blocks(text: str) -> list[str]:
    """将多文件 AST 文本按 'AST for:' 分隔符拆分为单文件块。"""
    if not text.strip():
        return []
    blocks = re.split(r'\n(?=AST for:)', text.strip())
    return [b.strip() for b in blocks if b.strip()]


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
        if context.tool_client is not None and self._include_broad_scan:
            resp = context.tool_client.find_sensitive_apis()
            if not getattr(resp, "success", False):
                diagnostics["sensitive_api"] = str(
                    getattr(resp, "error", "tool_failed") or "tool_failed"
                )
            else:
                content = resp.as_tool_output()
                clipped, truncated = _clip(content)
            if getattr(resp, "success", False) and content.strip():
                facts.append(
                    ContextFact(
                        source="tool:find_sensitive_apis",
                        kind="sensitive_api",
                        content=clipped,
                        truncated=truncated,
                    )
                )
                gathered.append(GatheredContext("find_sensitive_apis", "{}", content))

        # 4. AST 结构提取（diff 内文件）
        if context.tool_client is not None:
            resp = context.tool_client.get_diff_ast(context.diff_text)
            if not getattr(resp, "success", False):
                diagnostics["ast_structure"] = str(
                    getattr(resp, "error", "tool_failed") or "tool_failed"
                )
                content = ""
            else:
                content = (
                    resp.as_tool_output()
                    if hasattr(resp, "as_tool_output")
                    else str(resp)
                )
            if content.strip() and "无可解析" not in content:
                for file_block in _split_ast_blocks(content):
                    facts.append(ContextFact(
                        source="tool:get_diff_ast",
                        kind="ast_structure",
                        content=file_block,
                    ))
                gathered.append(GatheredContext("get_diff_ast", "{}", content))

        bundle = ContextBundle(
            changed_files=changed_files,
            facts=facts,
        )
        context.context_bundle = bundle
        context.context_diagnostics = diagnostics
        context.gathered_context.extend(gathered)
        fact_sources = sorted({fact.source for fact in facts} | {"diff"})
        logger.info(
            "管线阶段 [context_provider]:%d 个文件,%d 条事实,来源=%s",
            len(changed_files),
            len(facts),
            fact_sources,
        )
        return context
