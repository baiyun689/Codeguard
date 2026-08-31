"""读取 git diff。

阶段 1 只支持一种最简单的输入:本地 git 仓库的 diff。
后续阶段再扩展 GitHub PR diff 等来源。
"""

from __future__ import annotations

import re
import subprocess

_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")


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
    """把 unified diff 按文件拆成 {现文件相对路径: 该文件的 diff 片段}。

    用途:保留为通用 diff 工具,供测试、诊断或后续明确需要按文件查看 diff
    的场景复用。当前 ADR-032 发现者运行链路始终读取完整 diff,不再按文件裁剪。

    设计要点:
    - 以 `diff --git ` 行为分段边界,每段保留完整的文件头与 hunk。
    - 段的 key 优先取 `+++ b/<path>`，没有该头时退化到 `diff --git` 的新路径。
      因而纯重命名、二进制和仅 mode 变更也能稳定定位当前文件。
    - 删除文件的新文件头是 `+++ /dev/null`,没有"现文件"路径,跳过。
    - 确定性纯函数,可独立单测、不触发 IO;空 diff / 无法解析 → 返回空 dict。
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
