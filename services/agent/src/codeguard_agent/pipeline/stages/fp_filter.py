"""误报过滤阶段(两段式)。

接在聚合去重之后、产出最终结果之前,作为默认管线的最后一个 stage。

- **第一段:规则硬过滤**(零成本、确定性)——用 fp_rules 的确定性规则剔除明显误报。
- **第二段:LLM 验证**(可选、默认关闭)——对存活项逐条调 LLM 复核"是否真实问题"。

设计要点:
- 与现有 stage 一致是**同步** execute,不引入 async。
- 被过滤的 issue **从 context.issues 移除**(不给 Issue 加字段,保护 ADR-001);
  剔除统计写入 context.filter_stats,供 evals/诊断使用。
- 第二段对 LLM 返回 None / 调用失败做防御:一律**保留** issue(宁可漏过滤、不可误删真问题)。
- 门禁(CRITICAL→退出码)由 cli 基于过滤后的 result.issues 计算,移除即自动重算,无需额外处理。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from codeguard_agent.pipeline.fp_rules import FpRules, load_rules, match_exclusion
from codeguard_agent.pipeline.stages.base import PipelineContext, PipelineStage

logger = logging.getLogger("codeguard")

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "fp_verify.txt"


@dataclass
class FilterStats:
    """误报过滤的统计,写入 PipelineContext.filter_stats。"""

    total_input: int = 0
    removed_by_rules: int = 0
    removed_by_llm: int = 0
    surviving: int = 0
    rule_hits: dict[str, int] = field(default_factory=dict)


class FpVerdict(BaseModel):
    """第二段 LLM 验证对单条 issue 的判定。"""

    is_real_issue: bool = Field(description="这条是否是真实存在、值得报告的问题")
    reason: str = Field(default="", description="判定依据,便于人工复核")


def _load_verify_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


class FalsePositiveFilterStage(PipelineStage):
    """两段式误报过滤。

    参数:
        rules_path: 规则文件路径;None 用默认(config/false-positive-rules.yaml)。
        enable_llm_verification: 是否启用第二段 LLM 验证(默认关闭,零成本即可用)。
    """

    def __init__(
        self,
        rules_path: Path | None = None,
        enable_llm_verification: bool = False,
    ) -> None:
        self._rules: FpRules = load_rules(rules_path)
        self._verify = enable_llm_verification
        self._prompt_cache: str | None = None

    @property
    def name(self) -> str:
        return "false_positive_filter"

    def execute(self, context: PipelineContext) -> PipelineContext:
        stats = FilterStats(total_input=len(context.issues))

        # 第一段:确定性规则硬过滤
        survivors = []
        for issue in context.issues:
            rule_id = match_exclusion(issue, self._rules)
            if rule_id is None:
                survivors.append(issue)
            else:
                stats.removed_by_rules += 1
                stats.rule_hits[rule_id] = stats.rule_hits.get(rule_id, 0) + 1

        # 第二段:可选 LLM 验证(默认关闭;mock 模式无可用模型时也跳过)。
        # 优先用异源的 fp_verify_llm,避免审查器核查自己的结论(自我确认偏差,见 ADR-005)。
        if self._verify and survivors:
            verify_llm = context.fp_verify_llm or context.llm
            if verify_llm is not None:
                survivors = self._llm_verify(verify_llm, context, survivors, stats)

        stats.surviving = len(survivors)
        context.issues = survivors
        context.filter_stats = stats
        logger.info(
            "管线阶段 [%s]:输入 %d,规则剔除 %d,LLM 剔除 %d,存活 %d",
            self.name, stats.total_input, stats.removed_by_rules,
            stats.removed_by_llm, stats.surviving,
        )
        return context

    def _llm_verify(self, verify_llm, context, survivors, stats):
        """逐条调验证模型复核;判误报则剔除,None/失败一律保留。"""
        if self._prompt_cache is None:
            self._prompt_cache = _load_verify_prompt()
        structured = verify_llm.with_structured_output(
            FpVerdict, method=context.structured_method
        )
        kept = []
        for issue in survivors:
            prompt = (
                self._prompt_cache
                .replace("{{diff}}", context.diff_text)
                .replace("{{type}}", issue.type or "")
                .replace("{{file}}", f"{issue.file}:{issue.line}")
                .replace("{{message}}", issue.message or "")
            )
            try:
                verdict = structured.invoke([("human", prompt)])
            except Exception as exc:  # noqa: BLE001 验证失败不该误删真问题
                logger.warning("[fp_filter] LLM 验证调用失败,保留该 issue: %s", exc)
                kept.append(issue)
                continue
            # None 防御:结构化输出可能返回 None(见 §6.4)——保留,不误删
            if verdict is None or not isinstance(verdict, FpVerdict):
                logger.warning("[fp_filter] LLM 验证未返回有效结果,保留该 issue")
                kept.append(issue)
                continue
            if verdict.is_real_issue:
                kept.append(issue)
            else:
                stats.removed_by_llm += 1
        return kept
