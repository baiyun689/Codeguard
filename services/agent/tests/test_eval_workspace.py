from __future__ import annotations

import subprocess

from evals.schema import EvalCase
from evals.workspace import materialize_case_workspace, tool_server_repo_path


def _git(cwd, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_materialize_case_workspace_applies_diff_without_mutating_base(tmp_path):
    base = tmp_path / "repo"
    base.mkdir()
    _git(base, "init", "--initial-branch=main")
    (base / "Service.java").write_text("class Service { int value = 1; }\n", encoding="utf-8")
    _git(base, "add", "Service.java")
    _git(base, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base")

    (base / "Service.java").write_text("class Service { int value = 2; }\n", encoding="utf-8")
    diff = _git(base, "diff", "--", "Service.java") + "\n"
    _git(base, "restore", "--", "Service.java")

    case = EvalCase(
        id="workspace-test",
        category="logic",
        diff=diff,
        repo_path=str(base),
    )

    workspace = materialize_case_workspace(case, workspace_root=tmp_path / "workspaces")
    try:
        assert workspace.path != base
        assert (workspace.path / "Service.java").read_text(encoding="utf-8") == (
            "class Service { int value = 2; }\n"
        )
        assert (base / "Service.java").read_text(encoding="utf-8") == (
            "class Service { int value = 1; }\n"
        )
        assert _git(base, "status", "--porcelain") == ""
    finally:
        workspace.cleanup()

    assert not workspace.path.exists()


def test_tool_server_repo_path_maps_host_workspace_to_container(monkeypatch, tmp_path):
    host_root = tmp_path / "projects"
    workspace = host_root / "eval-workspaces" / "case-1"
    monkeypatch.setenv("CODEGUARD_PROJECTS_DIR", str(host_root))
    monkeypatch.setenv("CODEGUARD_TOOL_SERVER_PROJECT_ROOT", "/workspace/projects")

    assert tool_server_repo_path(workspace) == "/workspace/projects/eval-workspaces/case-1"


def test_tool_server_repo_path_preserves_native_path_without_mapping(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEGUARD_PROJECTS_DIR", raising=False)
    monkeypatch.delenv("CODEGUARD_TOOL_SERVER_PROJECT_ROOT", raising=False)
    workspace = tmp_path / "case-1"

    assert tool_server_repo_path(workspace) == str(workspace)
