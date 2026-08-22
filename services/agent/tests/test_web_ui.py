from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from codeguard_agent import web_ui


def test_safe_project_maps_host_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / "projects"
    (project_root / "demo").mkdir(parents=True)
    monkeypatch.setattr(web_ui, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(web_ui, "HOST_PROJECT_ROOT", r"E:\workspace")

    assert web_ui._safe_project(r"E:\workspace\demo") == (project_root / "demo").resolve()


def test_safe_project_rejects_unmounted_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / "projects"
    project_root.mkdir()
    monkeypatch.setattr(web_ui, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(web_ui, "HOST_PROJECT_ROOT", r"E:\workspace")
    with pytest.raises(ValueError):
        web_ui._safe_project(r"E:\other\demo")


def test_host_path_maps_report_to_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(web_ui, "PROJECT_ROOT", tmp_path / "projects")
    monkeypatch.setattr(web_ui, "HOST_PROJECT_ROOT", r"E:\workspace")

    result = web_ui._host_path(str(tmp_path / "projects" / "demo" / "reports" / "review.md"))

    assert result == r"E:\workspace\demo\reports\review.md"


def test_latest_report_returns_newest_markdown(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    old = reports / "old.md"
    new = reports / "new.md"
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    old.touch()
    new.touch()

    assert web_ui._latest_report(tmp_path) == str(new)


def test_git_bases_returns_head_and_recent_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        web_ui.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="abcdef123456\tabcdef1\tadd review fixture\n",
            stderr="",
        ),
    )

    assert web_ui._git_bases(tmp_path) == [
        {"value": "HEAD", "label": "HEAD（当前工作树）"},
        {"value": "abcdef123456", "label": "abcdef1 add review fixture"},
    ]


def test_parse_review_output_allows_report_message_after_json() -> None:
    result, trailing = web_ui._parse_review_output(
        '{"summary":"发现 1 个问题","issues":[]}\n报告已写入: /workspace/reports/review.md'
    )

    assert result == {"summary": "发现 1 个问题", "issues": []}
    assert trailing == "报告已写入: /workspace/reports/review.md"


def test_report_link_opens_web_endpoint() -> None:
    assert 'href="/api/jobs/\'+id+\'/report"' in web_ui.UI_HTML
    assert "打开报告目录" not in web_ui.UI_HTML
    assert "复制报告路径" not in web_ui.UI_HTML
