"""读取 git diff。

阶段 1 只支持一种最简单的输入:本地 git 仓库的 diff。
后续阶段再扩展 GitHub PR diff 等来源。
"""

from __future__ import annotations

import subprocess


def collect_diff(repo_path: str = ".", base: str = "HEAD") -> str:
    """采集本地 git 仓库的代码变更(diff 文本)。

    参数:
        repo_path: git 仓库路径,默认当前目录
        base: 对比基准。默认 'HEAD' 表示"工作区相对最近一次提交的改动"。
              也可传入分支名或提交号(如 'main')做分支间对比。

    返回:
        unified diff 格式的文本;没有任何改动时返回空字符串。

    说明:这里直接调用系统 git 命令而非用 GitPython 之类的库,
    是为了阶段 1 把依赖压到最少。后续如需更强的 diff 解析能力再换。
    """
    result = subprocess.run(
        ["git", "-C", repo_path, "diff", base],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff 执行失败: {result.stderr.strip()}")
    return result.stdout


def collect_staged_diff(repo_path: str = ".") -> str:
    """采集已暂存(git add 之后)的改动。

    适合在 commit 前做"提交前审查"的场景。
    """
    result = subprocess.run(
        ["git", "-C", repo_path, "diff", "--cached"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff --cached 执行失败: {result.stderr.strip()}")
    return result.stdout
