import json
from subprocess import CompletedProcess

import pytest

from codeguard_agent import web_ui


@pytest.mark.parametrize("code, status", [(0, "completed"), (1, "completed"), (2, "incomplete"), (3, "failed")])
def test_review_ui_preserves_incomplete_status(monkeypatch, tmp_path, code, status):
    monkeypatch.setattr(web_ui, "_safe_project", lambda _: tmp_path)
    monkeypatch.setattr(web_ui.subprocess, "run", lambda *a, **k: CompletedProcess(
        [], code, json.dumps({"issues": [], "summary": ""}), "diagnostic"
    ))
    monkeypatch.setattr(web_ui, "JOBS", {"test": {"status": "running"}})
    web_ui._run_review("test", "repo", "HEAD", False, False)
    assert web_ui.JOBS["test"]["status"] == status
    if code == 2:
        assert "不代表没有缺陷" in web_ui.JOBS["test"]["summary"]
