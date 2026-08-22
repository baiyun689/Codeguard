from __future__ import annotations

from pathlib import Path

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


def test_parse_review_output_allows_report_message_after_json() -> None:
    result, trailing = web_ui._parse_review_output(
        '{"summary":"发现 1 个问题","issues":[]}\n报告已写入: /workspace/reports/review.md'
    )

    assert result == {"summary": "发现 1 个问题", "issues": []}
    assert trailing == "报告已写入: /workspace/reports/review.md"
