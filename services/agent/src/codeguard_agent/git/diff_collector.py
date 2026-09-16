"""调用 Git 命令采集本地仓库变更，并提供按文件拆分 diff 的辅助函数。"""

from __future__ import annotations

import re
import subprocess

_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def collect_diff(repo_path: str = ".", base: str = "HEAD") -> str:
    """返回仓库相对指定基线的统一 diff 文本。

    repo_path 为仓库路径，base 可为分支、提交号或 HEAD。
    默认比较工作区与 HEAD；没有变更时返回空字符串。
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


def collect_head_revision(repo_path: str = ".") -> str:
    """返回当前工作树所属的完整 HEAD SHA，作为项目快照版本的基线。"""
    result = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD 执行失败: {result.stderr.strip()}")
    return result.stdout.strip()


def split_diff_by_file(diff_text: str) -> dict[str, str]:
    """按文件拆分统一 diff，返回当前文件路径到完整 diff 片段的映射。

    以 diff --git 为分段边界，保留文件头和全部变更块。
    优先使用 +++ b/ 中的新路径，缺失时使用 diff --git 中的新路径。
    删除文件不含当前文件路径，因此跳过；空输入返回空字典。
    """
    if not diff_text:
        return {}

    # 先按 `diff --git ` 切块;首个 `diff --git ` 之前的内容(正常 git diff 没有)忽略。
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                blocks.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append(current)

    def _current_path(block: list[str]) -> str | None:
        if any(line == "+++ /dev/null" or line.startswith("deleted file mode") for line in block):
            return None
        for line in block:
            if line.startswith("+++ b/"):
                return line[len("+++ b/"):].split("\t", 1)[0].strip()
        match = _DIFF_HEADER.match(block[0]) if block else None
        return match.group(2).strip() if match else None

    sections: dict[str, str] = {}
    for block in blocks:
        path = _current_path(block)
        if path:
            sections[path] = "\n".join(block)
    return sections
