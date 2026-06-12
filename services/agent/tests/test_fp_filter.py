"""误报过滤的工程正确性测试。

分两部分:
- 规则层(fp_rules):确定性匹配/路径排除/置信度阈值/缺文件回退——纯函数,死磕。
- 过滤阶段(FalsePositiveFilterStage):第一段移除+统计、第二段 LLM 验证的 None 防御。

LLM 的不确定性都用假对象隔离掉,这里只测确定性逻辑(对比 evals 量化真实质量)。
"""

from __future__ import annotations

from pathlib import Path

from codeguard_agent.models.schemas import Issue, Severity
from codeguard_agent.pipeline.fp_rules import FpRules, load_rules, match_exclusion
from codeguard_agent.pipeline.stages.base import PipelineContext
from codeguard_agent.pipeline.stages.fp_filter import (
    FalsePositiveFilterStage,
    FpVerdict,
)

_RULES_YAML = """
message_patterns:
  generic-refactor:
    - "consider refactoring"
    - "建议重构"
path_substrings:
  build-dirs:
    - "node_modules/"
path_filename_patterns:
  test-files:
    - "(^|/|_)test"
    - "test(s)?\\\\.(java|py)$"
  doc-files:
    - "\\\\.(md|txt)$"
confidence:
  auto_exclude_below: 0.5
"""


def _issue(severity=Severity.WARNING, file="src/A.java", line=10, type="空指针",
           message="msg", confidence=1.0) -> Issue:
    return Issue(severity=severity, file=file, line=line, type=type,
                 message=message, confidence=confidence)


def _rules(tmp_path: Path) -> FpRules:
    p = tmp_path / "fp.yaml"
    p.write_text(_RULES_YAML, encoding="utf-8")
    return load_rules(p)


# ---- 规则层 ----

def test_命中message正则被剔除(tmp_path):
    rules = _rules(tmp_path)
    hit = _issue(type="代码质量", message="Consider refactoring this method")
    assert match_exclusion(hit, rules) == "generic-refactor"


def test_命中中文message正则(tmp_path):
    rules = _rules(tmp_path)
    assert match_exclusion(_issue(message="建议重构该方法"), rules) == "generic-refactor"


def test_命中路径子串(tmp_path):
    rules = _rules(tmp_path)
    hit = _issue(file="frontend/node_modules/x/index.js")
    assert match_exclusion(hit, rules) == "build-dirs"


def test_命中测试文件名(tmp_path):
    rules = _rules(tmp_path)
    assert match_exclusion(_issue(file="src/UserServiceTest.java"), rules) == "test-files"


def test_命中文档扩展名(tmp_path):
    rules = _rules(tmp_path)
    assert match_exclusion(_issue(file="docs/README.md"), rules) == "doc-files"


def test_置信度低于阈值被剔除(tmp_path):
    rules = _rules(tmp_path)
    assert match_exclusion(_issue(confidence=0.3), rules) == "low-confidence"


def test_置信度高于阈值且无其它命中_保留(tmp_path):
    rules = _rules(tmp_path)
    # 真问题:高置信度、普通源码路径、具体描述——不应命中任何规则
    keep = _issue(file="src/main/java/UserDao.java", type="SQL注入",
                  message="name 直接拼进 SQL", confidence=0.95)
    assert match_exclusion(keep, rules) is None


def test_规则文件缺失_视为无规则不剔除(tmp_path):
    rules = load_rules(tmp_path / "不存在.yaml")
    assert rules.is_empty
    assert match_exclusion(_issue(message="consider refactoring", confidence=0.1), rules) is None


def test_空pattern不会编译成匹配一切(tmp_path):
    # 空列表若被 re.compile("") 会匹配一切——防御:应被跳过
    p = tmp_path / "empty.yaml"
    p.write_text("message_patterns:\n  bad: []\n", encoding="utf-8")
    rules = load_rules(p)
    assert match_exclusion(_issue(message="任意文本"), rules) is None


# ---- 过滤阶段:第一段(规则硬过滤) ----

def _ctx(issues, llm=None):
    c = PipelineContext(diff_text="diff", llm=llm)
    c.issues = list(issues)
    return c


def _stage(tmp_path, enable_llm=False):
    p = tmp_path / "fp.yaml"
    p.write_text(_RULES_YAML, encoding="utf-8")
    return FalsePositiveFilterStage(rules_path=p, enable_llm_verification=enable_llm)


def test_第一段剔除命中规则项并记统计(tmp_path):
    stage = _stage(tmp_path)
    noise = _issue(type="质量", message="consider refactoring this")
    real = _issue(type="SQL注入", message="name 拼进 SQL", confidence=0.95)
    ctx = stage.execute(_ctx([noise, real]))
    assert ctx.issues == [real]
    assert ctx.filter_stats.removed_by_rules == 1
    assert ctx.filter_stats.surviving == 1
    assert ctx.filter_stats.rule_hits.get("generic-refactor") == 1


def test_mock模式跳过第二段(tmp_path):
    # llm=None(mock):即便开了开关,也不该发起 LLM 调用,只走第一段
    stage = _stage(tmp_path, enable_llm=True)
    ctx = stage.execute(_ctx([_issue(confidence=0.95)], llm=None))
    assert ctx.filter_stats.removed_by_llm == 0
    assert len(ctx.issues) == 1


def test_过滤后门禁基于存活集合(tmp_path):
    # 低置信度的 CRITICAL 被规则剔除后,存活集合里不应再有 CRITICAL
    stage = _stage(tmp_path)
    weak_critical = _issue(severity=Severity.CRITICAL, confidence=0.2)
    ctx = stage.execute(_ctx([weak_critical]))
    assert all(i.severity != Severity.CRITICAL for i in ctx.issues)


# ---- 过滤阶段:第二段(可选 LLM 验证) ----

class _FakeStructured:
    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self._i = 0

    def invoke(self, _messages):
        v = self._verdicts[self._i]
        self._i += 1
        return v


class _FakeLLM:
    """按顺序对每条 issue 返回预设裁定(FpVerdict 或 None)。"""

    def __init__(self, verdicts):
        self._verdicts = verdicts
        self.called = False

    def with_structured_output(self, _model, method=None):
        self.called = True
        return _FakeStructured(self._verdicts)


def test_第二段默认关闭不发起调用(tmp_path):
    stage = _stage(tmp_path, enable_llm=False)
    llm = _FakeLLM([])
    stage.execute(_ctx([_issue(confidence=0.95)], llm=llm))
    assert llm.called is False


def test_第二段判误报则剔除(tmp_path):
    stage = _stage(tmp_path, enable_llm=True)
    keep, drop = _issue(type="真问题", confidence=0.95), _issue(type="噪音", confidence=0.95)
    llm = _FakeLLM([FpVerdict(is_real_issue=True), FpVerdict(is_real_issue=False)])
    ctx = stage.execute(_ctx([keep, drop], llm=llm))
    assert ctx.issues == [keep]
    assert ctx.filter_stats.removed_by_llm == 1


def test_第二段返回None则保留(tmp_path):
    # None 防御:验证失败不该误删真问题
    stage = _stage(tmp_path, enable_llm=True)
    issue = _issue(confidence=0.95)
    llm = _FakeLLM([None])
    ctx = stage.execute(_ctx([issue], llm=llm))
    assert ctx.issues == [issue]
    assert ctx.filter_stats.removed_by_llm == 0
