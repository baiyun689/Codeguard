"""ContextProvider 项目语义图接入单测。"""

from codeguard_agent.pipeline.context.provider import _changed_locations


def test_changed_locations_groups_hunks_by_file():
    diff = """\
diff --git a/Foo.java b/Foo.java
--- a/Foo.java
+++ b/Foo.java
@@ -1 +1,2 @@
 class Foo {}
+class Bar {}
@@ -9 +10 @@
-old
+new
"""

    assert _changed_locations(diff) == [
        {"file": "Foo.java", "lines": [2, 10]},
    ]


def test_changed_locations_empty_diff():
    assert _changed_locations("") == []
