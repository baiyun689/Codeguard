"""聚合第二段(LLM 语义综合)的工程正确性测试。

LLM 的"判断哪些同源"是不确定的(那由 evals 量化),但本阶段把不确定性收敛到一件确定的事:
**给定分组方案,如何确定性地合并、以及各种异常如何回退**。这部分适合用 pytest 死磕。

打桩 LLM:不发起真实调用,直接喂入预设的 _MergePlan,验证合并/回退逻辑。
"""

from __future__ import annotations


from codeguard_agent.models.schemas import Issue, Severity
from codeguard_agent.legacy.stages.aggregation import (
    AggregationStage,
    _MergeGroup,
    _MergePlan,
    _apply_merge_plan,
)
from codeguard_agent.pipeline.context.base import PipelineContext


def _issue(severity=Severity.WARNING, file="A.java", line=10, type="SQL注入",
           message="msg", confidence=1.0) -> Issue:
    return Issue(severity=severity, file=file, line=line, type=type,
                 message=message, confidence=confidence)


# ---------------------------------------------------------------------------
# _apply_merge_plan:确定性合并逻辑
# ---------------------------------------------------------------------------


def test_近邻同源分组_合并为一条且保留最高severity():
    # 同一处 SQL 注入,两个审查员报在相邻行、措辞不同
    issues = [
        _issue(severity=Severity.WARNING, line=68, message="这里可能有注入"),
        _issue(severity=Severity.CRITICAL, line=69, message="字符串拼接 SQL,存在注入"),
    ]
    plan = _MergePlan(groups=[_MergeGroup(members=[1, 2])])
    out = _apply_merge_plan(issues, plan)
    assert len(out) == 1
    assert out[0].severity == Severity.CRITICAL


def test_无分组_全部原样保留():
    issues = [_issue(line=10), _issue(line=99, type="资源泄漏")]
    out = _apply_merge_plan(issues, _MergePlan(groups=[]))
    assert len(out) == 2


def test_不同问题不会被合并_只要LLM没分到一组():
    # 同一行的不同种类问题:LLM 未把它们分到一组 → 保持两条
    issues = [_issue(line=10, type="空指针"), _issue(line=10, type="硬编码")]
    out = _apply_merge_plan(issues, _MergePlan(groups=[]))
    assert len(out) == 2


def test_单成员分组被忽略_不构成合并():
    issues = [_issue(line=10), _issue(line=20)]
    out = _apply_merge_plan(issues, _MergePlan(groups=[_MergeGroup(members=[1])]))
    assert len(out) == 2


def test_越界序号被忽略():
    issues = [_issue(line=10), _issue(line=20)]
    # 序号 3 越界:有效成员只剩 1 个 → 不合并
    out = _apply_merge_plan(issues, _MergePlan(groups=[_MergeGroup(members=[1, 3])]))
    assert len(out) == 2


def test_重叠分组_先到先得():
    issues = [_issue(line=10), _issue(line=11), _issue(line=12)]
    # 第一组占用 1、2;第二组想要 2、3,但 2 已被占,只剩 3 个有效成员不足 → 第二组失效
    plan = _MergePlan(groups=[_MergeGroup(members=[1, 2]), _MergeGroup(members=[2, 3])])
    out = _apply_merge_plan(issues, plan)
    assert len(out) == 2  # {1,2} 合一 + 独立的 3


def test_合并在最早成员位置输出_保留相对顺序():
    issues = [
        _issue(line=10, type="A"),
        _issue(line=20, type="B"),
        _issue(line=30, type="C"),
    ]
    # 合并 1 和 3,代表是第 3 条(CRITICAL),应出现在第 1 条的位置
    issues[2] = _issue(severity=Severity.CRITICAL, line=30, type="C")
    plan = _MergePlan(groups=[_MergeGroup(members=[1, 3])])
    out = _apply_merge_plan(issues, plan)
    assert [i.type for i in out] == ["C", "B"]


# ---------------------------------------------------------------------------
# AggregationStage.execute:打桩 LLM,验证两段协同与回退
# ---------------------------------------------------------------------------


class _FakeStructured:
    def __init__(self, plan, raises=False):
        self._plan = plan
        self._raises = raises

    def invoke(self, messages):
        if self._raises:
            raise RuntimeError("boom")
        return self._plan


class _FakeLLM:
    """最小打桩:with_structured_output 返回一个会吐预设 plan(或抛异常)的对象。"""

    def __init__(self, plan=None, raises=False):
        self._plan = plan
        self._raises = raises
        self.called = False

    def with_structured_output(self, model, method="function_calling"):
        self.called = True
        return _FakeStructured(self._plan, self._raises)


def _ctx(issues, llm) -> PipelineContext:
    return PipelineContext(diff_text="x", llm=llm, issues=list(issues))


def test_stage_第二段合并近邻同源():
    issues = [
        _issue(severity=Severity.WARNING, line=68),
        _issue(severity=Severity.CRITICAL, line=69),
    ]
    llm = _FakeLLM(plan=_MergePlan(groups=[_MergeGroup(members=[1, 2])]))
    ctx = AggregationStage().execute(_ctx(issues, llm))
    assert len(ctx.issues) == 1
    assert ctx.issues[0].severity == Severity.CRITICAL


def test_stage_LLM返回None时回退第一段结果():
    issues = [_issue(line=68), _issue(line=69)]
    llm = _FakeLLM(plan=None)
    ctx = AggregationStage().execute(_ctx(issues, llm))
    # 两条行号不同、规则去重保留两条;LLM None → 回退,仍是两条
    assert len(ctx.issues) == 2


def test_stage_LLM调用异常时回退():
    issues = [_issue(line=68), _issue(line=69)]
    llm = _FakeLLM(raises=True)
    ctx = AggregationStage().execute(_ctx(issues, llm))
    assert len(ctx.issues) == 2


def test_stage_mock模式不发起第二段():
    issues = [_issue(line=68), _issue(line=69)]
    ctx = AggregationStage().execute(_ctx(issues, llm=None))
    assert len(ctx.issues) == 2  # 只跑了规则去重


def test_stage_少于两条不发起第二段():
    llm = _FakeLLM(raises=True)  # 若被调用会抛异常
    ctx = AggregationStage().execute(_ctx([_issue()], llm))
    assert len(ctx.issues) == 1
    assert llm.called is False


def test_stage_第一段精确重复仍零成本合并():
    # 完全相同指纹(同文件同行同类型)由第一段直接合并,第二段不必介入
    issues = [_issue(line=10), _issue(line=10, message="措辞不同")]
    llm = _FakeLLM(plan=_MergePlan(groups=[]))
    ctx = AggregationStage().execute(_ctx(issues, llm))
    assert len(ctx.issues) == 1
