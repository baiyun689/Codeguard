"""任务拆分、DirectGate 与 diff 规模路由的纯函数。"""

from __future__ import annotations

import logging
import re

from codeguard_agent.git.diff_collector import split_diff_by_file
from codeguard_agent.models.tasks import (
    DiffMetrics,
    ReviewBudget,
    ReviewMode,
    ReviewTask,
    TaskRoute,
)

logger = logging.getLogger("codeguard")

# 构建产物的目录前缀和文件后缀——这些文件不是源代码，审查它们毫无意义。
_BUILD_DIR_PREFIXES = (
    "target/",
    "build/",
    "out/",
    "dist/",
    "node_modules/",
    ".gradle/",
    "__pycache__/",
    ".mvn/",
    "bin/",
)
_IDE_METADATA_NAMES = (".classpath", ".project")
_NON_SOURCE_SUFFIXES = (
    ".class",
    ".jar",
    ".war",
    ".ear",
    ".zip",
    ".tar",
    ".gz",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".svg",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".min.js",
    ".min.css",
    ".map",
)

_DIRECT_DOCUMENT_SUFFIXES = (".md", ".mdx", ".adoc", ".rst", ".txt")
_DIRECT_DENY_TERMS = (
    "auth", "permission", "role", "token", "password", "secret", "csrf", "cors",
    "sql", "jdbc", "query", "transaction", "@transactional", "http", "url", "uri",
    "api", "controller", "request", "response", "serialize", "deserialize", "json",
    "yaml", "yml", "xml", "pom.xml", "build.gradle", "dependency", "synchronized",
    "lock", "thread", "executor", "async", "await", "cache", "kafka", "rabbit",
    "message", "stream", "file", "path", "upload", "download", "processbuilder",
)


def _is_build_artifact(file_path: str) -> bool:
    """判断文件是否为构建产物或二进制文件，不应作为审查任务。"""
    normalized = file_path.replace("\\", "/").lower()
    if normalized == ".idea" or normalized.startswith(".idea/"):
        return True
    basename = normalized.rsplit("/", 1)[-1]
    if basename in _IDE_METADATA_NAMES or basename.endswith(".iml"):
        return True
    for prefix in _BUILD_DIR_PREFIXES:
        if normalized.startswith(prefix):
            return True
    for suffix in _NON_SOURCE_SUFFIXES:
        if normalized.endswith(suffix):
            return True
    # Maven/Gradle 生成的列表文件
    if normalized.endswith("/createdfiles.lst") or normalized.endswith(
        "/inputfiles.lst"
    ):
        return True
    return False


def _changed_content_lines(patch: str) -> list[str]:
    """提取 diff 中真正新增/删除的内容行，排除 diff 元数据。"""
    lines: list[str] = []
    for line in patch.splitlines():
        if line.startswith(("+++", "---", "@@", "diff --git", "index ")):
            continue
        if line.startswith(("+", "-")):
            lines.append(line[1:])
    return lines


def _is_comment_or_blank(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith(("//", "/*", "*", "*/", "#", "<!--", "-->", ";"))


def classify_task_route(task: ReviewTask) -> TaskRoute:
    """确定性判断 task 是否可以走无工具 Direct 路径。

    规则故意保守：无法证明是文档/注释/空白变更时一律 Full。
    """
    normalized_path = task.file.replace("\\", "/").lower()
    patch_lower = task.patch.lower()
    if task.patch.count("\n") > 80:
        return TaskRoute(task_id=task.id, route="full", reason="task_too_large")
    if any(term in patch_lower or term in normalized_path for term in _DIRECT_DENY_TERMS):
        return TaskRoute(task_id=task.id, route="full", reason="semantic_or_runtime_change")

    changed = _changed_content_lines(task.patch)
    if normalized_path.endswith(_DIRECT_DOCUMENT_SUFFIXES):
        return TaskRoute(task_id=task.id, route="direct", reason="documentation_only")
    if changed and all(_is_comment_or_blank(line) for line in changed):
        return TaskRoute(task_id=task.id, route="direct", reason="comment_or_whitespace_only")
    return TaskRoute(task_id=task.id, route="full", reason="code_change_requires_full_review")


def classify_task_routes(tasks: list[ReviewTask]) -> dict[str, TaskRoute]:
    """为每个 ReviewTask 生成稳定的 Direct/Full 路由。"""
    return {task.id: classify_task_route(task) for task in tasks}


# @@ -oldStart[,oldLen] +newStart[,newLen] @@ [section heading]
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def _norm(path: str) -> str:
    """整条路径归一化（正斜杠 + 小写），用于全路径精确匹配。"""
    return (path or "").replace("\\", "/").lower()


def _basename(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]


def file_matches_task(file: str, task: ReviewTask) -> bool:
    """候选文件是否属于该 task 的文件（全路径精确匹配优先，退化到 basename）。

    单 task 调用不再做行号级映射（prompt 只含这一个 task），但仍需要
    这道最基本的一致性校验，防止模型报告了完全无关的文件却被直接绑定到该 task。
    """
    if task.file == "<whole-diff>":
        return bool(file.strip())
    return _norm(file) == _norm(task.file) or _basename(file) == _basename(task.file)


def _iter_diff_blocks(diff_text: str) -> list[list[str]]:
    """按 `diff --git ` 边界把 diff 切成块（每块是行列表）。"""
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
    return blocks


def _old_path(block: list[str]) -> str | None:
    """从 diff 块提取旧文件路径：优先 `--- a/<path>`，退化到 `diff --git a/<path> b/`。"""
    for line in block:
        if line.startswith("--- a/"):
            return line[len("--- a/") :].split("\t", 1)[0].strip()
    m = re.match(r"^diff --git a/(.+?) b/", block[0]) if block else None
    return m.group(1).strip() if m else None


def _fallback_targets(diff_text: str) -> dict[str, str]:
    """扫描 split_diff_by_file 会漏掉的块，返回需要文件级 fallback 的 {path: section}。

    覆盖两类 split_diff_by_file 刻意跳过（无 `+++ b/`）的变更：
    - 删除文件（`+++ /dev/null` / `deleted file mode`）→ 取旧路径。
    - 纯重命名（有 `rename to` 且无 `+++ b/`，即无内容变更）→ 取新路径。
    删除鉴权/校验/事务代码时，reviewer 仍能把候选绑定到该文件（spec §4.2）。
    """
    targets: dict[str, str] = {}
    for block in _iter_diff_blocks(diff_text):
        is_deletion = any(
            line == "+++ /dev/null" or line.startswith("deleted file mode")
            for line in block
        )
        has_plus_header = any(line.startswith("+++ b/") for line in block)
        rename_to = next(
            (
                line[len("rename to ") :].strip()
                for line in block
                if line.startswith("rename to ")
            ),
            None,
        )
        if is_deletion:
            path = _old_path(block)
            if path:
                targets[path] = "\n".join(block)
        elif rename_to and not has_plus_header:
            targets[rename_to] = "\n".join(block)
    return targets


def _split_hunks(section: str) -> list[tuple[str, str, int]]:
    """把单文件 diff 片段切成 [(header_line, hunk_body, new_start_line)]。"""
    hunks: list[tuple[str, str, int]] = []
    current: list[str] | None = None
    header = ""
    new_start = 0
    for line in section.splitlines():
        m = _HUNK_HEADER.match(line)
        if m:
            if current is not None:
                hunks.append((header, "\n".join(current), new_start))
            header = line
            new_start = int(m.group(1))
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        hunks.append((header, "\n".join(current), new_start))
    return hunks


def _changed_lines(hunk_body: str, new_start: int) -> list[int]:
    """返回 hunk 中新增行('+')在新文件中的行号。"""
    changed: list[int] = []
    line_no = new_start
    for line in hunk_body.splitlines():
        if (
            line.startswith("@@")
            or line.startswith("+++")
            or line.startswith("---")
            or line.startswith(
                "\\ "
            )  # `\ No newline at end of file` 非文件行，不占行号
        ):
            continue
        if line.startswith("+"):
            changed.append(line_no)
            line_no += 1
        elif line.startswith("-"):
            continue  # 删除行不占新文件行号
        else:
            line_no += 1  # 上下文行
    return changed


def _hunk_span(task: ReviewTask) -> tuple[int, int] | None:
    """从 task.hunk_header 解析该 hunk 覆盖的新文件行范围 [start, end]。

    无 hunk_header（文件级 fallback task）返回 None。
    """
    m = _HUNK_HEADER.match(task.hunk_header or "")
    if not m:
        return None
    start = int(m.group(1))
    length = int(m.group(2)) if m.group(2) else 1
    return (start, start + max(length, 1) - 1)


def build_tasks(diff_text: str) -> list[ReviewTask]:
    """解析 unified diff → ReviewTask 列表。

    - 有内容变更的文件：每 hunk 一个 task；无 hunk 时退化为该文件的文件级 fallback。
    - 删除文件 / 纯重命名：split_diff_by_file 会漏掉，补一个文件级 fallback task。
    """
    tasks: list[ReviewTask] = []
    seen_files: set[str] = set()
    skipped_artifacts = 0
    for file, section in split_diff_by_file(diff_text).items():
        if _is_build_artifact(file):
            skipped_artifacts += 1
            continue
        seen_files.add(file)
        hunks = _split_hunks(section)
        if not hunks:
            tasks.append(
                ReviewTask(
                    id=f"{file}#file", file=file, patch=section, changed_lines=[]
                )
            )
            continue
        for i, (header, body, new_start) in enumerate(hunks):
            tasks.append(
                ReviewTask(
                    id=f"{file}#h{i}",
                    file=file,
                    hunk_header=header,
                    patch=body,
                    changed_lines=_changed_lines(body, new_start),
                )
            )
    for path, section in _fallback_targets(diff_text).items():
        if path in seen_files or _is_build_artifact(path):
            continue
        tasks.append(
            ReviewTask(id=f"{path}#file", file=path, patch=section, changed_lines=[])
        )
    if skipped_artifacts:
        logger.info("build_tasks: 跳过 %d 个构建产物文件", skipped_artifacts)
    return tasks


def build_file_tasks(diff_text: str) -> list[ReviewTask]:
    """解析 unified diff → 每文件一个 ReviewTask。

    - 有内容变更的文件：合并该文件所有 hunk 为一个 task，patch 为完整文件级 diff section
    - 删除文件 / 纯重命名：文件级 fallback task
    """
    tasks: list[ReviewTask] = []
    seen_files: set[str] = set()
    skipped_artifacts = 0
    for file, section in split_diff_by_file(diff_text).items():
        if _is_build_artifact(file):
            skipped_artifacts += 1
            continue
        seen_files.add(file)
        hunks = _split_hunks(section)
        if not hunks:
            tasks.append(
                ReviewTask(
                    id=f"{file}#file", file=file, patch=section, changed_lines=[]
                )
            )
            continue
        # 收集所有 hunk 的变更行
        all_changed: list[int] = []
        for _header, body, new_start in hunks:
            all_changed.extend(_changed_lines(body, new_start))
        tasks.append(
            ReviewTask(
                id=f"{file}#file",
                file=file,
                patch=section,
                changed_lines=all_changed,
            )
        )
    for path, section in _fallback_targets(diff_text).items():
        if path in seen_files or _is_build_artifact(path):
            continue
        tasks.append(
            ReviewTask(id=f"{path}#file", file=path, patch=section, changed_lines=[])
        )
    if skipped_artifacts:
        logger.info("build_file_tasks: 跳过 %d 个构建产物文件", skipped_artifacts)
    return tasks


def build_whole_diff_task(diff_text: str) -> list[ReviewTask]:
    """SMALL 模式保持单 task，但仍进入统一 ReviewCouncil 管线。"""
    sections = {
        file: section
        for file, section in split_diff_by_file(diff_text).items()
        if not _is_build_artifact(file)
    }
    files = list(sections)
    filtered_diff = "\n".join(sections.values())
    changed_lines: list[int] = []
    for section in sections.values():
        for _header, body, new_start in _split_hunks(section):
            changed_lines.extend(_changed_lines(body, new_start))
    return [
        ReviewTask(
            id="whole_diff#task",
            file=files[0] if len(files) == 1 else "<whole-diff>",
            patch=filtered_diff,
            changed_lines=changed_lines,
            patch_complete=False,
        )
    ] if filtered_diff.strip() else []


def diff_metrics(diff_text: str) -> DiffMetrics:
    """返回 PR 规模路由使用的稳定、轻量统计。"""
    file_count = sum(
        line.startswith("diff --git ")
        for line in diff_text.splitlines()
    )
    if file_count == 0:
        file_count = len(split_diff_by_file(diff_text))
    return DiffMetrics(
        file_count=file_count,
        hunk_count=len(_HUNK_HEADER.findall(diff_text)),
        diff_chars=len(diff_text),
    )


def classify_diff(diff_text: str, budget: ReviewBudget) -> ReviewMode:
    """根据 diff 文本体量决定审查模式（不构建 ReviewTask，轻量统计）。

    纯确定性函数：只扫描 diff 文本的文件数/hunk 数/字符数。
    不调 LLM，不读仓库文件，不建 task 对象。

    判定逻辑（字符数主导——核心问题是 diff 能否装进上下文窗口）：
    - small：文件数、hunk 数、diff 字符数均不超过对应阈值
    - medium：不超过中型阈值，否则
    - large：超出中型阈值
    """
    metrics = diff_metrics(diff_text)
    diff_chars = metrics.diff_chars
    file_count = metrics.file_count
    hunk_count = metrics.hunk_count

    if (
        file_count <= budget.small_max_files
        and hunk_count <= budget.small_max_hunks
        and diff_chars <= budget.small_max_diff_chars
    ):
        return ReviewMode.SMALL

    if (
        file_count <= budget.medium_max_files
        and diff_chars <= budget.medium_max_diff_chars
    ):
        return ReviewMode.MEDIUM

    return ReviewMode.LARGE


# ── 噪声过滤：diff 元信息不构成审查问题 ──────────────────────────────

# 噪声类型关键词（匹配 issue.type 或 issue.message）
_NOISE_TYPE_KEYWORDS = (
    "copyright",
    "版权",
    "license",
    "许可证",
    "@author",
    "import",
    "whitespace",
    "空白",
    "格式",
    "formatting",
    "注释",
    "comment",
)


def is_noise_issue(
    file: str,
    issue_type: str,
    message: str = "",
) -> bool:
    """判定一个 issue 是否属于 diff 元信息噪声，不应进入后续管线。

    只过滤：版权/@author/import/注释/空白变更。
    不拦截任何与测试文件相关的问题——diff 包含测试文件时，审查员对
    测试代码的发现是正当审查产出。
    """
    type_lower = issue_type.lower()
    msg_lower = message.lower()

    # 版权/@author/import/注释/空白 → 噪声
    if any(kw in type_lower for kw in _NOISE_TYPE_KEYWORDS):
        return True
    if any(kw in msg_lower for kw in ("版权", "copyright", "@author", "import 语句")):
        return True

    return False
