"""判分器(matcher)的工程正确性测试。

判分由两段组成:**配对**(规则尺确定性 / 裁判尺由 LLM 给,本测试用假裁判注入)+
**据配对算分**(_build_outcome,纯确定性)。这两段都该死磕,LLM 本身的语义质量交给 evals。
重点验证:据配对算分对不对、裁判脏数据(越界/重复 id)能不能被挡住、clean 不调裁判。
"""

from __future__ import annotations

from codeguard_agent.models.schemas import Issue, Severity

from evals.matcher import _build_outcome, _llm_pairing, _rule_pairing, evaluate_case
from evals.schema import CaseJudgement, EvalCase, ExpectedIssue, JudgeMatch


def _issue(file="A.java", line=10, type="空指针", message="msg", severity=Severity.WARNING) -> Issue:
    return Issue(severity=severity, file=file, line=line, type=type, message=message, confidence=1.0)


def _expected(keywords=("空指针", "npe"), file="A.java", line=10, severity=None) -> ExpectedIssue:
    return ExpectedIssue(type_keywords=list(keywords), file=file, line=line, severity=severity)


def _case(expected, diff="--- diff ---", is_clean_id="c1") -> EvalCase:
    return EvalCase(id=is_clean_id, category="test", diff=diff, expected=expected)


class _FakeJudge:
    """假裁判:with_structured_output(...).invoke(...) 固定返回给定的 CaseJudgement。"""

    def __init__(self, judgement: CaseJudgement):
        self._judgement = judgement

    def with_structured_output(self, *_args, **_kwargs):
        return self

    def invoke(self, *_args, **_kwargs):
        return self._judgement


# ---- 规则尺配对 ----

def test_规则配对_贪心命中():
    case = _case([_expected(line=10)])
    reported = [_issue(line=10)]
    assert _rule_pairing(case, reported) == {0: 0}


def test_规则配对_关键词不撞则不命中():
    case = _case([_expected(keywords=("资源泄漏",), line=10)])
    reported = [_issue(type="空指针", message="msg", line=10)]
    assert _rule_pairing(case, reported) == {}


def test_规则配对_一条报告不被两条标准答案重复占用():
    case = _case([_expected(line=10), _expected(line=10)])
    reported = [_issue(line=10)]  # 只有一条报告
    pairing = _rule_pairing(case, reported)
    assert pairing == {0: 0}  # 第二条标准答案没得配 → 漏报


# ---- 据配对算分 ----

def test_算分_tp_fp_fn():
    case = _case([_expected(line=10), _expected(line=20)])
    reported = [_issue(line=10), _issue(line=99, type="无关")]  # 一条命中,一条多余
    outcome = _build_outcome(case, reported, {0: 0}, "rule")
    assert outcome.true_positives == 1
    assert outcome.false_negatives == 1   # 第二条标准答案没配上
    assert outcome.false_positives == 1   # 多余那条报告


def test_算分_定位与级别命中():
    case = _case([_expected(line=10, severity=Severity.CRITICAL)])
    reported = [_issue(line=12, severity=Severity.CRITICAL)]  # 行差2,容差默认3 → 定位命中
    outcome = _build_outcome(case, reported, {0: 0}, "rule")
    assert outcome.localization_hits == 1
    assert outcome.severity_checked == 1
    assert outcome.severity_hits == 1


def test_算分_级别判错记入诊断():
    case = _case([_expected(line=10, severity=Severity.CRITICAL)])
    reported = [_issue(line=10, severity=Severity.WARNING)]
    outcome = _build_outcome(case, reported, {0: 0}, "rule")
    assert outcome.severity_hits == 0
    assert outcome.severity_detail[0]["match"] == "✗"


# ---- 裁判尺:脏数据防御 ----

def test_裁判配对_越界编号被丢弃():
    case = _case([_expected(line=10)])
    reported = [_issue(line=10)]
    judge = _FakeJudge(CaseJudgement(matches=[JudgeMatch(expected_id=0, reported_id=5)]))  # 越界
    assert _llm_pairing(judge, case, reported) == {}


def test_裁判配对_重复认领同一报告只算首次():
    case = _case([_expected(file="A.java", line=10), _expected(file="A.java", line=20)])
    reported = [_issue(line=10)]
    judge = _FakeJudge(CaseJudgement(matches=[
        JudgeMatch(expected_id=0, reported_id=0),
        JudgeMatch(expected_id=1, reported_id=0),  # 想再认领同一条 → 丢弃
    ]))
    assert _llm_pairing(judge, case, reported) == {0: 0}


def test_裁判配对_负一表示漏报():
    case = _case([_expected(line=10)])
    reported = [_issue(line=10)]
    judge = _FakeJudge(CaseJudgement(matches=[JudgeMatch(expected_id=0, reported_id=-1)]))
    assert _llm_pairing(judge, case, reported) == {}


# ---- evaluate_case 整合 ----

def test_clean_用例报告全计误报且不调裁判():
    case = _case([], is_clean_id="clean1")  # 无标准答案 = clean
    reported = [_issue(), _issue(line=20)]
    # 传一个会抛错的"裁判",证明 clean 根本不会调它
    class _Boom:
        def with_structured_output(self, *a, **k): raise AssertionError("clean 不该调裁判")
    outcome = evaluate_case(case, reported, judge_llm=_Boom())
    assert outcome.false_positives == 2
    assert outcome.primary_judge == "rule"


def test_vuln_用例走裁判主判且回填规则交叉校验():
    # 规则尺因关键词不撞而漏配,裁判语义命中 → 两尺应当分歧
    case = _case([_expected(keywords=("资源泄漏",), file="A.java", line=10)])
    reported = [_issue(type="未关闭流", message="FileInputStream 未关闭", line=10)]
    judge = _FakeJudge(CaseJudgement(matches=[JudgeMatch(expected_id=0, reported_id=0)]))
    outcome = evaluate_case(case, reported, judge_llm=judge)
    assert outcome.primary_judge == "llm"
    assert outcome.true_positives == 1          # 裁判判命中
    assert outcome.rule_true_positives == 0     # 规则尺漏配
    assert outcome.rule_false_positives == 1


def test_裁判失败回退规则尺():
    case = _case([_expected(line=10)])
    reported = [_issue(line=10)]
    class _Failing:
        def with_structured_output(self, *a, **k): return self
        def invoke(self, *a, **k): raise RuntimeError("judge down")
    outcome = evaluate_case(case, reported, judge_llm=_Failing())
    assert outcome.primary_judge == "rule"
    assert outcome.true_positives == 1


# ---- 诱饵归类 + severity 分层(eval-complex-behavior) ----

from evals.schema import Distractor  # noqa: E402


def test_诱饵归类_踩中诱饵记中诱饵其余记凭空乱报():
    # 标答 1 条(命中),另有 2 条误报:一条踩诱饵、一条凭空乱报。
    case = EvalCase(
        id="cx", category="混合", diff="d",
        expected=[_expected(file="A.java", line=10)],
        distractors=[Distractor(type_keywords=["硬编码"], file="A.java", line=40, note="常量非密钥")],
    )
    reported = [
        _issue(file="A.java", line=10),                       # TP
        _issue(file="A.java", line=40, type="硬编码密钥"),     # 踩诱饵
        _issue(file="A.java", line=99, type="无关乱报"),       # 凭空乱报
    ]
    outcome = _build_outcome(case, reported, {0: 0}, "rule")
    assert outcome.true_positives == 1
    assert outcome.false_positives == 2          # FP 总数不因归类改变
    assert outcome.distractor_total == 1
    assert outcome.distractor_hits == 1          # 只有踩诱饵那条
    # 凭空乱报 = FP - 中诱饵
    assert outcome.false_positives - outcome.distractor_hits == 1


def test_诱饵归类_命中优先于诱饵():
    # 报告同时匹配某标答与某诱饵(同位置同词)→ 计 TP,不计中诱饵。
    case = EvalCase(
        id="cx", category="混合", diff="d",
        expected=[_expected(keywords=("硬编码",), file="A.java", line=40)],
        distractors=[Distractor(type_keywords=["硬编码"], file="A.java", line=40, note="撞位置")],
    )
    reported = [_issue(file="A.java", line=40, type="硬编码")]
    outcome = _build_outcome(case, reported, {0: 0}, "rule")
    assert outcome.true_positives == 1
    assert outcome.false_positives == 0
    assert outcome.distractor_hits == 0          # 已被标答认领,不算被骗


def test_诱饵归类_无诱饵时total与hits为零():
    case = _case([_expected(line=10)])
    reported = [_issue(line=10), _issue(line=99, type="乱报")]
    outcome = _build_outcome(case, reported, {0: 0}, "rule")
    assert outcome.distractor_total == 0
    assert outcome.distractor_hits == 0
    assert outcome.false_positives == 1


def test_severity_分层_主次落桶():
    case = EvalCase(
        id="cx", category="混合", diff="d",
        expected=[
            _expected(keywords=("sql",), file="A.java", line=10, severity=Severity.CRITICAL),  # 主,命中
            _expected(keywords=("npe",), file="A.java", line=20, severity=Severity.WARNING),    # 次,命中
            _expected(keywords=("魔法",), file="A.java", line=30, severity=Severity.INFO),       # 次,漏
            _expected(keywords=("注入",), file="A.java", line=50, severity=Severity.CRITICAL),  # 主,漏
        ],
    )
    reported = [
        _issue(file="A.java", line=10, type="sql注入"),
        _issue(file="A.java", line=20, type="npe空指针"),
    ]
    outcome = _build_outcome(case, reported, {0: 0, 1: 1}, "rule")
    assert (outcome.tp_primary, outcome.fn_primary) == (1, 1)
    assert (outcome.tp_secondary, outcome.fn_secondary) == (1, 1)


def test_severity_分层_未标级别不计分层():
    case = _case([_expected(line=10, severity=None)])  # 无 severity
    reported = [_issue(line=10)]
    outcome = _build_outcome(case, reported, {0: 0}, "rule")
    assert outcome.true_positives == 1
    assert (outcome.tp_primary, outcome.tp_secondary) == (0, 0)
    assert (outcome.fn_primary, outcome.fn_secondary) == (0, 0)
