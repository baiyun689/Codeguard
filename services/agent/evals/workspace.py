"""评测用例的目标工作区物化。

repo-backed case 保存的是干净基线快照，``case.diff`` 才是本次要审查的变更。
工具服务必须读取应用 diff 后的临时工作区，不能直接读取基线目录。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import stat
import subprocess
import tempfile
from uuid import uuid4

from evals.schema import EvalCase


@dataclass
class MaterializedWorkspace:
    """应用当前 case diff 后的临时 Git clone。"""

    path: Path

    def cleanup(self) -> None:
        """清理本次生成的明确目录，不触碰基线快照。"""
        if self.path.exists():
            import shutil

            shutil.rmtree(self.path, onerror=_remove_readonly)


def materialize_case_workspace(
    case: EvalCase,
    *,
    workspace_root: Path | None = None,
) -> MaterializedWorkspace:
    """从 repo-backed 基线创建并应用当前 case 的 diff。

    原始 ``case.repo_path`` 永远不被修改。默认使用 Gateway 默认允许的
    ``<系统临时目录>/codeguard-jobs`` 下的短路径，避免 Windows MAX_PATH；
    自定义 Gateway allowed roots 时通过 ``CODEGUARD_EVAL_WORKSPACE_DIR`` 指定
    与其匹配的目录。
    """
    base_repo = Path(case.repo_path).resolve()
    if not (base_repo / ".git").exists():
        raise ValueError(f"case repo 不是 Git 快照:{base_repo}")

    configured_root = os.environ.get("CODEGUARD_EVAL_WORKSPACE_DIR", "").strip()
    # 使用短路径避免 Windows Git checkout 因源码相对路径较深而超过 MAX_PATH。
    # 默认落在 Gateway 默认的 codeguard-jobs 根目录下；自定义 Gateway
    # allowed roots 时，调用方应通过 CODEGUARD_EVAL_WORKSPACE_DIR 对齐该根目录。
    parent = workspace_root.resolve() if workspace_root is not None else (
        Path(configured_root).resolve() if configured_root else (
        Path(tempfile.gettempdir()) / "codeguard-jobs" / "eval-workspaces"
        )
    )
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / f"{case.id}-{uuid4().hex[:12]}"

    try:
        _run_git(
            base_repo,
            ["clone", "--local", "--no-hardlinks", str(base_repo), str(target)],
        )
        _run_git(
            target,
            [
                "apply",
                "--index",
                "--whitespace=nowarn",
                "--ignore-whitespace",
                "-",
            ],
            input_text=case.diff,
        )
    except Exception:
        MaterializedWorkspace(target).cleanup()
        raise

    return MaterializedWorkspace(target)


def _run_git(
    cwd: Path,
    args: list[str],
    *,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
        raise RuntimeError(f"git {' '.join(args)} 失败:{detail}")
    return result


def _remove_readonly(function, path, _exc_info) -> None:
    """让 Windows 能删除 clone 中由 Git 写入的只读对象文件。"""
    os.chmod(path, stat.S_IWRITE)
    function(path)
