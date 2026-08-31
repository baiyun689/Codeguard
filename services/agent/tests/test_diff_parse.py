"""unified diff 分段解析的工程正确性测试。"""

from __future__ import annotations

from codeguard_agent.git.diff_collector import split_diff_by_file

# ---------------------------------------------------------------------------
# split_diff_by_file:按文件拆分 diff(摘要驱动的按域裁剪用)
# ---------------------------------------------------------------------------

_MULTI_FILE_DIFF = (
    "diff --git a/src/App.java b/src/App.java\n"
    "index 111..222 100644\n"
    "--- a/src/App.java\n"
    "+++ b/src/App.java\n"
    "@@ -1 +1,2 @@\n"
    " class App {}\n"
    "+// changed\n"
    "diff --git a/src/Util.java b/src/Util.java\n"
    "--- a/src/Util.java\n"
    "+++ b/src/Util.java\n"
    "@@ -1 +1 @@\n"
    "-a\n+b\n"
)


def test_split_按现文件路径拆分且键与parse一致():
    sections = split_diff_by_file(_MULTI_FILE_DIFF)
    assert set(sections.keys()) == {"src/App.java", "src/Util.java"}
    # 每段保留自己的 diff --git 头与新增行
    assert sections["src/App.java"].startswith("diff --git a/src/App.java")
    assert "+// changed" in sections["src/App.java"]
    assert "+b" in sections["src/Util.java"]
    # 段与段不串味:App 段不含 Util 的内容
    assert "Util.java" not in sections["src/App.java"]


def test_split_空diff返回空dict():
    assert split_diff_by_file("") == {}
    assert split_diff_by_file("no diff headers") == {}


def test_split_删除文件无现文件路径_跳过():
    diff = (
        "diff --git a/src/Gone.java b/src/Gone.java\n"
        "deleted file mode 100644\n"
        "--- a/src/Gone.java\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-bye\n"
    )
    assert split_diff_by_file(diff) == {}


def test_split_无文本hunk的现文件仍保留完整diff块():
    diff = (
        "diff --git a/logo.png b/logo.png\n"
        "index 111..222 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n"
    )
    sections = split_diff_by_file(diff)
    assert set(sections) == {"logo.png"}
    assert "Binary files" in sections["logo.png"]
