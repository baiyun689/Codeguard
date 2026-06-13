"""diff → 改动文件集合解析(parse_changed_files)的工程正确性测试。

这是喂给工具沙箱的 allowed_files 来源,确定性纯函数,适合用 pytest 死磕。
"""

from __future__ import annotations

from codeguard_agent.git.diff_collector import parse_changed_files


def test_多文件_diff_解析出所有现文件路径():
    diff = (
        "diff --git a/src/App.java b/src/App.java\n"
        "index 111..222 100644\n"
        "--- a/src/App.java\n"
        "+++ b/src/App.java\n"
        "@@ -1 +1 @@\n"
        "-old\n+new\n"
        "diff --git a/src/Util.java b/src/Util.java\n"
        "--- a/src/Util.java\n"
        "+++ b/src/Util.java\n"
        "@@ -1 +1 @@\n"
        "-a\n+b\n"
    )
    assert parse_changed_files(diff) == ["src/App.java", "src/Util.java"]


def test_空_diff_返回空列表():
    assert parse_changed_files("") == []
    assert parse_changed_files("   \n  ") == []


def test_无文件头的文本_返回空列表():
    assert parse_changed_files("just some text\nno diff headers here") == []


def test_删除文件_不计入因为没有现文件可读():
    # 删除文件的新文件头是 `+++ /dev/null`,不应被算作可读文件。
    diff = (
        "diff --git a/src/Gone.java b/src/Gone.java\n"
        "deleted file mode 100644\n"
        "--- a/src/Gone.java\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-bye\n"
    )
    assert parse_changed_files(diff) == []


def test_去重且排序():
    diff = (
        "+++ b/z.java\n"
        "+++ b/a.java\n"
        "+++ b/a.java\n"
    )
    assert parse_changed_files(diff) == ["a.java", "z.java"]
