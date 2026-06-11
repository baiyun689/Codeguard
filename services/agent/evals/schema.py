"""评测框架的数据结构。

三组模型:
    - ExpectedIssue / EvalCase：数据集这一侧(我已经知道答案的样本)
    - MatchOutcome：单条用例跑完后的判定结果(TP/FP/FN 明细)
    - CaseMetrics / AggregateMetrics：由判定结果聚合出来的指标

设计要点:expected 用"关键词列表 + 行号 + 容差"做弱约束,而不是要求 LLM 一字不差。
代码审查的"对错"本身有模糊地带,过严的匹配会把指标做成噪音。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from codeguard_agent.models.schemas import Severity


class ExpectedIssue(BaseModel):
    """一条标准答案:这段 diff 里"应该被审出来"的一个问题。

    匹配判定(见 matcher.py)默认满足三条即算命中:
      1. 文件名对得上(按 basename 或后缀);
      2. 行号落在 [line - tolerance, line + tolerance] 内(line=0 时跳过行号判定);
      3. 报告的 type/message 命中 type_keywords 里任一关键词(忽略大小写)。
    """

    type_keywords: list[str] = Field(
        description="问题类型关键词,报告命中其一即视为类型对上,如 ['sql', '注入', 'injection']"
    )
    file: str = Field(description="问题所在文件(按 basename / 后缀匹配,无需完整路径)")
    line: int = Field(default=0, description="期望行号;0 表示不校验行号")
    tolerance: int = Field(default=3, description="行号容差,LLM 报的行号常有偏移")
    severity: Severity | None = Field(
        default=None, description="期望级别(弱约束,仅用于统计级别准确率,不影响命中)"
    )
    note: str = Field(default="", description="给人看的说明,如'用户输入直接拼进 SQL'")


class EvalCase(BaseModel):
    """一条评测用例 = 一段 diff + 它的标准答案。

    vuln 样本:expected 非空(应被检出的问题清单)。
    clean 样本:expected 为空——任何被报出来的问题都计入"误报"。
    """

    id: str = Field(description="用例唯一标识,如 'sql_injection_001'")
    category: str = Field(description="类别,如 'SQL注入' / 'clean'")
    dimension: str = Field(
        default="security",
        description="审查维度:security / logic / quality(clean 样本随便标)。"
        "阶段 2 起按维度拆分 recall,衡量各领域审查员的价值。",
    )
    language: str = Field(default="java", description="代码语言")
    description: str = Field(default="", description="这条用例考的是什么")
    diff: str = Field(description="unified diff 文本,喂给 reviewer.review() 的输入")
    expected: list[ExpectedIssue] = Field(
        default_factory=list, description="标准答案;clean 样本留空"
    )

    @property
    def is_clean(self) -> bool:
        """是否为无问题样本(专门用来量误报)。"""
        return len(self.expected) == 0


class JudgeScore(BaseModel):
    """LLM-as-judge 对一条"命中的报告"的质量打分(可选,仅 --judge 时产生)。"""

    semantic_match: bool = Field(description="语义上是否真的命中了这条标准答案")
    message_quality: int = Field(ge=1, le=5, description="问题描述质量 1~5")
    suggestion_quality: int = Field(ge=1, le=5, description="修复建议质量 1~5(无建议给 1)")
    comment: str = Field(default="", description="评审简评")


class MatchOutcome(BaseModel):
    """单条用例跑一次审查后的判定结果。"""

    case_id: str
    is_clean: bool
    true_positives: int = Field(default=0, description="命中的标准答案数")
    false_negatives: int = Field(default=0, description="漏掉的标准答案数")
    false_positives: int = Field(default=0, description="报了但对不上任何标准答案的数量")
    expected_total: int = Field(default=0, description="该用例标准答案总数")
    reported_total: int = Field(default=0, description="该用例报告问题总数")
    localization_hits: int = Field(default=0, description="命中项里行号也对上的数量")
    severity_hits: int = Field(default=0, description="命中项里级别也对上的数量(仅标了 severity 的)")
    severity_checked: int = Field(default=0, description="参与级别校验的命中项数量")
    severity_detail: list[dict[str, str]] = Field(
        default_factory=list,
        description="逐项级别诊断:每个参与校验的命中项的 期望级别 vs 报告级别,便于定位是哪几条判错",
    )
    judge_scores: list[JudgeScore] = Field(default_factory=list, description="LLM 质量打分明细")


class CaseMetrics(BaseModel):
    """单条用例在 N 次重复跑测下的指标(均值)。"""

    case_id: str
    category: str
    is_clean: bool
    runs: int
    detection_rate: float = Field(description="vuln:平均检出率 TP/expected;clean:恒为 1")
    avg_false_positives: float = Field(description="平均误报数")
    recall_mean: float = 0.0
    recall_std: float = 0.0


class AggregateMetrics(BaseModel):
    """整个数据集的聚合指标——这就是要被固化成 baseline 的那组数字。"""

    runs: int
    num_cases: int
    num_vuln_cases: int
    num_clean_cases: int

    precision: float = Field(description="报出的问题里真问题占比 TP/(TP+FP)")
    recall: float = Field(description="标准答案被检出占比 TP/(TP+FN)")
    f1: float = Field(description="precision 与 recall 的调和平均")

    false_positives_on_clean: float = Field(description="干净样本上平均每条 diff 误报几个")
    localization_accuracy: float = Field(description="命中项里行号也对上的比例")
    severity_accuracy: float = Field(description="命中项里级别也对上的比例")

    recall_std: float = Field(default=0.0, description="recall 在多次跑测间的标准差")
    precision_std: float = Field(default=0.0, description="precision 在多次跑测间的标准差")

    avg_judge_message_quality: float | None = Field(
        default=None, description="LLM-as-judge:命中项 message 平均分(未启用为 None)"
    )
    avg_judge_suggestion_quality: float | None = Field(
        default=None, description="LLM-as-judge:命中项 suggestion 平均分(未启用为 None)"
    )
