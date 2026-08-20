"""CLI `review --report` 的工程正确性测试。

报告生成挂 CLI 执行路径(本地审查);CI(GitHub App)链路调 `--format json`
不带 `--report`,天然不生成——本模块不涉及 Gateway 侧改动。

测试用 monkeypatch 掐断 LLM/编排器(不碰网络、不建工具会话),
只验证:--report 时在 <repo>/reports/ 落盘带时间戳的报告并打印路径,
不带 --report 时不落盘。
"""

from __future__ import annotations

from codeguard_agent import cli
from codeguard_agent.models.schemas import Issue, ReviewResult, Severity

_DIFF = (
    "diff --git a/src/App.java b/src/App.java\n"
    "--- a/src/App.java\n"
    "+++ b/src/App.java\n"
    "@@ -1,2 +1,3 @@\n"
    " lineA\n"
    " lineB\n"
    "+exec(cmd);\n"
)


class _FakeOrchestrator:
    """替身编排器:立即返回固定结果,不跑真实管线。"""

    def __init__(self, **kwargs: object) -> None:
        pass

    def run(self, *args: object, **kwargs: object) -> ReviewResult:
        return ReviewResult(
            summary="有注入风险",
            issues=[
                Issue(
                    severity=Severity.CRITICAL,
                    file="App.java",
                    line=3,
                    type="OS 命令注入",
                    message="外部输入拼进 shell 命令",
                    confidence=0.92,
                )
            ],
        )


def _patch_cli(monkeypatch) -> None:
    """掐断 diff 采集、LLM 构建与编排器,使 review 命令零网络跑通。"""
    monkeypatch.setattr(cli, "collect_diff", lambda repo, base: _DIFF)
    monkeypatch.setattr(cli, "build_llm", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "PipelineOrchestrator", _FakeOrchestrator)


def test_report_落盘到repo下reports目录并打印路径(tmp_path, monkeypatch, capsys):
    _patch_cli(monkeypatch)
    exit_code = cli.main(["review", "--repo", str(tmp_path), "--report"])
    assert exit_code == 1  # 存在 CRITICAL → 门禁退出码

    reports_dir = tmp_path / "reports"
    files = list(reports_dir.glob("review-report-*.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "# 🔍 Codeguard 审查报告" in content
    assert "OS 命令注入" in content
    assert "```java" in content            # diff 可提取时代码片段嵌入

    out = capsys.readouterr().out
    assert "报告已写入" in out
    assert str(files[0]) in out


def test_不带report_不生成报告(tmp_path, monkeypatch, capsys):
    _patch_cli(monkeypatch)
    cli.main(["review", "--repo", str(tmp_path)])
    assert not (tmp_path / "reports").exists()
    out = capsys.readouterr().out
    assert "报告已写入" not in out
