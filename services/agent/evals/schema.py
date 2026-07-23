"""评测框架的数据结构。

三组模型:
    - ExpectedIssue / EvalCase：数据集这一侧(我已经知道答案的样本)
    - MatchOutcome：单条用例跑完后的判定结果(TP/FP/FN 明细)
    - CaseMetrics / AggregateMetrics：由判定结果聚合出来的指标

设计要点:expected 用"关键词列表 + 行号 + 容差"做弱约束,而不是要求 LLM 一字不差。
代码审查的"对错"本身有模糊地带,过严的匹配会把指标做成噪音。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field, field_validator

from codeguard_agent.models.schemas import Severity

logger = logging.getLogger("codeguard.evals")

# 能力标签:一条用例"审准它至少需要哪类上下文",对应工具背后的地面真值来源分层。
#   diff-only  仅看 diff 即可判定
#   file       需读改动文件之外的整文件(get_file_content)
#   repo-map   需先定位"diff 调用的符号定义在哪个跨文件"再细读(get_repo_map 导航 + get_file_content)
#   ast        需单文件结构/方法签名(未来 get_method_definition)
#   call-graph 需跨文件调用/影响关系(未来 get_call_graph / get_related_files)
#   rag        需按语义检索项目别处实现(未来 semantic_search)
VALID_CAPABILITIES = ("diff-only", "file", "repo-map", "ast", "call-graph", "rag")


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


class Distractor(BaseModel):
    """一个"诱饵":diff 里**看着像漏洞、实则无害**的点(已校验的拼接、用了安全 API 的反序列化……)。

    与 `ExpectedIssue` 同构,以复用 matcher 的文件/行号/关键词匹配函数。语义相反:
    审查器若报到这里,就是**被诱饵骗了**——计入误报,并归类为"中诱饵"(区别于"凭空乱报")。
    专门用来量复杂脏代码下审查器的克制力(见 eval-complex-behavior spec)。
    """

    file: str = Field(description="诱饵所在文件(按 basename / 后缀匹配)")
    line: int = Field(default=0, description="诱饵行号;0 表示不校验行号")
    tolerance: int = Field(default=3, description="行号容差")
    type_keywords: list[str] = Field(
        description="审查器若误报此处大概率会用的关键词,命中其一 + 文件/行号对上即判为'中诱饵'"
    )
    note: str = Field(default="", description="为什么这是诱饵而非真问题(给人看,造数据时务必写清)")


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
    diff: str = Field(description="unified diff 文本,喂给审查管线的输入")
    expected: list[ExpectedIssue] = Field(
        default_factory=list, description="标准答案;clean 样本留空"
    )
    repo_path: str = Field(
        default="",
        description="repo-backed 用例的仓库根路径(指向快照 repo/ 目录,代表变更后的工程状态);"
        "空表示纯内联合成用例(磁盘无对应文件,工具读不到)。",
    )
    capability: list[str] = Field(
        default_factory=lambda: ["diff-only"],
        description="能力标签:审准本用例至少需要哪类上下文(见 VALID_CAPABILITIES)。"
        "仅用于评测归类与切片,绝不改变审查链路行为。缺省为 ['diff-only']。",
    )
    distractors: list[Distractor] = Field(
        default_factory=list,
        description="诱饵清单:看着像漏洞、实则无害的点。报告踩中即归类为'中诱饵'误报,"
        "用来量复杂用例下的克制力。缺省为空(老用例向后兼容)。",
    )

    @field_validator("capability", mode="before")
    @classmethod
    def _normalise_capability(cls, value):
        """归一化能力标签:过滤非法值并告警,去重保序;为空则退回 ['diff-only']。"""
        if not value:
            return ["diff-only"]
        if isinstance(value, str):
            value = [value]
        seen: list[str] = []
        for raw in value:
            tag = str(raw).strip().lower()
            if tag not in VALID_CAPABILITIES:
                logger.warning("忽略非法能力标签 %r(合法取值:%s)", raw, ", ".join(VALID_CAPABILITIES))
                continue
            if tag not in seen:
                seen.append(tag)
        return seen or ["diff-only"]

    @property
    def is_clean(self) -> bool:
        """是否为无问题样本(专门用来量误报)。"""
        return len(self.expected) == 0

    @property
    def is_repo_backed(self) -> bool:
        """是否为 repo-backed 用例(带真实可读的仓库快照)。"""
        return bool(self.repo_path)

    @property
    def is_complex(self) -> bool:
        """是否为复杂用例(一份 diff 植入多条真问题)。用于复杂度切片与裁判可信契约。"""
        return len(self.expected) > 1


class JudgeMatch(BaseModel):
    """裁判对**一条标准答案**的命中判定(案例级 LLM 判分的最小单元)。

    裁判只做语义配对这件难事:把报告对到标准答案;TP/FP/FN/级别 由代码据此确定性算出
    (见 matcher.py)。这样裁判的不确定性被限制在"配对"一处,其余仍可复现。
    """

    expected_id: int = Field(description="标准答案编号(prompt 里 [E#] 的 #)")
    reported_id: int = Field(
        default=-1, description="命中它的报告编号([R#] 的 #);-1 表示无人命中(漏报)"
    )
    reason: str = Field(default="", description="判定依据,便于人工复核")


class CaseJudgement(BaseModel):
    """裁判对一条用例的整体判定:逐条标准答案给出是否被命中、被哪条命中。"""

    matches: list[JudgeMatch] = Field(
        default_factory=list, description="对每条标准答案各一条 match"
    )
    comment: str = Field(default="", description="整体简评")


class JudgeScore(BaseModel):
    """LLM-as-judge 对一条"命中的报告"的质量打分(旧逐对打分模型,暂留作兼容)。"""

    semantic_match: bool = Field(description="语义上是否真的命中了这条标准答案")
    message_quality: int = Field(ge=1, le=5, description="问题描述质量 1~5")
    suggestion_quality: int = Field(ge=1, le=5, description="修复建议质量 1~5(无建议给 1)")
    comment: str = Field(default="", description="评审简评")


class ToolUsage(BaseModel):
    """一条用例一次审查里,审查员实际发起的工具调用画像(可观测性,不参与判分)。

    源数据是管线汇总**去重后**的 gathered_context(按(工具,参数)去重、且仅含有返回内容的调用),
    故 tool_calls 是"去重后取得有效上下文的调用条数",不是原始调用次数。

    存在意义(ADR-022):before/after 都 3/3 时,要能分辨审查员是**真调工具导航**、
    还是**纯靠 diff 推理蒙对**——尤其 callers 段到底有没有被读到。
    """

    tool_calls: int = Field(default=0, description="去重后取得有效上下文的工具调用条数")
    tools_used: list[str] = Field(default_factory=list, description="用到的工具名(去重排序)")
    repomap_called: bool = Field(default=False, description="是否调用过 get_repo_map")
    repomap_caller_section_read: bool = Field(
        default=False,
        description="get_repo_map 返回里是否含'直接调用方(callers)'段(callers 段被实际读到)",
    )
    files_read: list[str] = Field(
        default_factory=list, description="经 get_file_content 读取的文件路径(去重排序)"
    )


class CouncilTraceStats(BaseModel):
    """ADR-032/Phase 5 ReviewCouncil 过程统计(可观测性,不参与判分)。"""

    candidate_count: int = 0
    candidate_count_by_agent: dict[str, int] = Field(default_factory=dict)
    raw_candidate_count: int = Field(default=0, description="归并前的原始候选数")
    candidate_dedup_removed_count: int = Field(default=0, description="归并移除的候选数")
    candidate_dedup_llm_calls: int = Field(default=0, description="归并 LLM 调用次数")
    candidate_dedup_block_failure_count: int = Field(default=0, description="归并失败块数")
    evidence_request_count: int = Field(default=0, description="累计有效/无效证据请求数")
    truncated_candidates: int = Field(default=0, description="发现阶段因候选上限被截断的数量")
    verdict_count: int = Field(default=0, description="Judge 产生的候选裁决数")
    removed_by_judge: int = Field(default=0, description="Judge 候选裁决为 drop 的数量")
    removed_by_fp_rules: int = 0
    removed_by_fp_llm: int = 0
    no_support_candidate_count: int = 0
    no_support_retained_count: int = 0
    direct_counter_candidate_count: int = Field(
        default=0, description="具备 counter+direct+contradicts finding 的候选数"
    )
    direct_counter_retained_count: int = Field(
        default=0, description="上述直接反证候选中仍映射到最终 Issue 的数量"
    )
    direct_counter_retained_rate: float | None = Field(
        default=None, description="直接反证候选保留率；无此类候选时为 None"
    )
    all_insufficient_candidate_count: int = Field(
        default=0, description="关联 finding 非空且全部 insufficient 的候选数"
    )
    all_insufficient_retained_count: int = Field(
        default=0, description="全 insufficient 候选中仍映射到最终 Issue 的数量"
    )
    all_insufficient_retained_rate: float | None = Field(
        default=None, description="全 insufficient 候选保留率；无此类候选时为 None"
    )
    severity_defaulted_count: int = 0
    critical_candidate_count: int = 0
    critical_policy_matched_count: int = 0
    critical_missing_factor_count: int = 0
    severity_transitions: dict[str, int] = Field(default_factory=dict)
    final_issue_count: int = Field(default=0, description="最终 Issue 对应的 survivor 候选数")
    final_issue_strategy_covered_count: int = Field(
        default=0, description="survivor 中至少关联一条有效 EvidenceRequest 的数量"
    )
    final_issue_strategy_coverage: float | None = Field(
        default=None, description="最终 Issue 的有效策略覆盖率；无最终 Issue 时为 None"
    )
    final_issue_fact_covered_count: int = Field(
        default=0, description="survivor 中至少有关联非 insufficient finding 的数量"
    )
    final_issue_fact_coverage: float | None = Field(
        default=None, description="最终 Issue 的有效事实覆盖率；无最终 Issue 时为 None"
    )
    registry_risk_tag_covered_count: int = Field(
        default=0, description="静态注册表同时具有 counter/support/severity 策略的 RiskTag 数"
    )
    registry_risk_tag_total: int = Field(default=0, description="当前 RiskTag 枚举值总数")
    registry_risk_tag_coverage: float | None = Field(
        default=None, description="静态注册表 RiskTag 三目的策略覆盖率"
    )
    actual_evidence_tool_calls: int = Field(
        default=0, description="EvidenceAgent 实际发起的新工具调用数；缓存复用不计"
    )
    average_evidence_tool_calls: float = Field(
        default=0.0, description="实际新证据工具调用数/候选数；无候选时为 0.0"
    )
    trace_events: int = 0


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
    judge_scores: list[JudgeScore] = Field(default_factory=list, description="LLM 质量打分明细(旧路径,暂留)")

    # ---- 过度上报诊断:把误报拆成"被诱饵骗"和"凭空乱报"----
    distractor_total: int = Field(default=0, description="该用例埋的诱饵总数")
    distractor_hits: int = Field(
        default=0, description="误报里命中诱饵的数量(被骗);其余 FP 即'凭空乱报'= FP - distractor_hits"
    )

    # ---- severity 分层 TP/FN:量"抓大漏小"(主=CRITICAL,次=WARNING/INFO;None 不计分层)----
    tp_primary: int = Field(default=0, description="命中的主项(CRITICAL)标准答案数")
    fn_primary: int = Field(default=0, description="漏掉的主项标准答案数")
    tp_secondary: int = Field(default=0, description="命中的次项(WARNING/INFO)标准答案数")
    fn_secondary: int = Field(default=0, description="漏掉的次项标准答案数")

    # ---- 规则尺交叉校验(仅当本用例用了 LLM 裁判时才有对比意义)----
    primary_judge: str = Field(
        default="rule",
        description="本用例主判由谁出:rule=纯规则;llm=LLM 裁判语义配对",
    )
    rule_true_positives: int = Field(default=0, description="规则尺判出的 TP(交叉校验用)")
    rule_false_positives: int = Field(default=0, description="规则尺判出的 FP(交叉校验用)")
    rule_false_negatives: int = Field(default=0, description="规则尺判出的 FN(交叉校验用)")

    # ---- 工具使用画像(可观测性,不参与判分)----
    # 仅工具档且本条确有工具调用时非空;无工具/mock/未调工具为 None(报告/归档据此跳过)。
    tool_usage: ToolUsage | None = Field(
        default=None, description="审查员实际工具调用画像;无工具活动为 None"
    )
    council_trace: CouncilTraceStats | None = Field(
        default=None, description="ADR-032 ReviewCouncil 过程统计;无元数据时为 None"
    )


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

    # ---- 行为诊断指标族(eval-complex-behavior,全部加法;不适用时为 None → 报告渲染 "—")----
    distractor_hit_rate: float | None = Field(
        default=None, description="诱饵命中率 = Σ中诱饵 / Σ诱饵总数;无诱饵用例时 None"
    )
    vuln_noise_per_case: float = Field(
        default=0.0, description="vuln 噪音/条 = vuln 用例 FP 总数 / vuln 用例数(脏代码上的噪音,区别于 clean 误报率)"
    )
    report_inflation: float = Field(
        default=0.0, description="报告膨胀比 = vuln 用例上 报告数/标答数 的均值"
    )
    recall_primary: float | None = Field(
        default=None, description="主项(CRITICAL)recall;无主项标答时 None"
    )
    recall_secondary: float | None = Field(
        default=None, description="次项(WARNING/INFO)recall;无次项标答时 None"
    )
    severity_accuracy_complex: float | None = Field(
        default=None, description="复杂用例(标答>1)子集上的级别准确率;无此类命中时 None"
    )
    judge_rule_agreement: float | None = Field(
        default=None, description="裁判↔规则一致率 = 两尺判定全等的 LLM 主判用例数 / LLM 主判用例数;无 LLM 主判时 None"
    )
