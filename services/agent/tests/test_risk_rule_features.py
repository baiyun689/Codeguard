"""Tests for deterministic diff direction feature extraction."""

from __future__ import annotations

from codeguard_agent.models.tasks import ReviewTask
from codeguard_agent.pipeline.risk_rules.features import DiffFeatures, extract_features


def test_extracts_added_only_hunk_with_new_file_line_numbers():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        hunk_header="@@ -1,2 +1,3 @@",
        patch="@@ -1,2 +1,3 @@\n context\n+added\n tail\n",
    )

    assert extract_features(task) == DiffFeatures(
        path="A.java",
        added_lines=((2, "added"),),
        deleted_lines=(),
        context_lines=("context", "tail"),
        has_added=True,
        has_deleted=False,
        has_changed=False,
    )


def test_extracts_deleted_only_hunk():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        hunk_header="@@ -2,2 +2,1 @@",
        patch="@@ -2,2 +2,1 @@\n-old\n context\n",
    )

    features = extract_features(task)

    assert features.deleted_lines == ("old",)
    assert features.added_lines == ()
    assert features.has_deleted is True
    assert features.has_added is False
    assert features.has_changed is False


def test_extracts_replacement_as_both_directions():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        hunk_header="@@ -3 +3 @@",
        patch="@@ -3 +3 @@\n-old\n+new\n",
    )

    features = extract_features(task)

    assert features.added_lines == ((3, "new"),)
    assert features.deleted_lines == ("old",)
    assert features.has_changed is True


def test_uses_review_task_file_as_path():
    task = ReviewTask(
        id="src/A.java#h0",
        file="src/A.java",
        hunk_header="@@ -1 +1 @@",
        patch="@@ -1 +1 @@\n-old\n+new\n",
    )

    assert extract_features(task).path == "src/A.java"


def test_excludes_diff_metadata_from_all_text_collections():
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        hunk_header="@@ -1 +1 @@",
        patch=(
            "diff --git a/A.java b/A.java\n"
            "--- a/A.java\n"
            "+++ b/A.java\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "\\ No newline at end of file\n"
        ),
    )

    features = extract_features(task)

    assert features.added_lines == ((1, "new"),)
    assert features.deleted_lines == ("old",)
    assert features.context_lines == ()


def test_extracts_deleted_protection_lines_from_no_hunk_fallback():
    task = ReviewTask(
        id="Auth.java#file",
        file="Auth.java",
        patch=(
            "diff --git a/Auth.java b/Auth.java\n"
            "deleted file mode 100644\n"
            "--- a/Auth.java\n"
            "+++ /dev/null\n"
            "-if (isAdmin) allow();\n"
            "-checkPermission();\n"
        ),
    )

    features = extract_features(task)

    assert features.deleted_lines == ("if (isAdmin) allow();", "checkPermission();")
    assert features.has_deleted is True
    assert features.has_added is False


def test_fallback_tracks_deletion_context_and_addition_without_hunk_header():
    task = ReviewTask(
        id="Auth.java#file",
        file="Auth.java",
        patch="--- a/Auth.java\n+++ b/Auth.java\n-old\n context\n+new\n",
        changed_lines=[11],
    )

    features = extract_features(task)

    assert features.deleted_lines == ("old",)
    assert features.context_lines == ("context",)
    assert features.added_lines == ((11, "new"),)
    assert features.has_changed is True
