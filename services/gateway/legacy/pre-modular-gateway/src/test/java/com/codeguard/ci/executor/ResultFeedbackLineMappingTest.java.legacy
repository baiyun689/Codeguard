package com.codeguard.ci.executor;

import com.codeguard.ci.github.GitHubClient;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

class ResultFeedbackLineMappingTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Test
    void annotationsOnlyIncludeLocationsOnDiffNewSide() throws Exception {
        String diff = """
            diff --git a/Foo.java b/Foo.java
            --- a/Foo.java
            +++ b/Foo.java
            @@ -10,2 +10,3 @@
              context
            + added
              tail
            """;
        List<JsonNode> issues = new ArrayList<>();
        issues.add(issue("Foo.java", 10, "WARNING", "context issue"));
        issues.add(issue("Foo.java", 11, "CRITICAL", "added issue"));
        issues.add(issue("Foo.java", 99, "WARNING", "outside hunk"));
        issues.add(issue("Other.java", 11, "WARNING", "unknown file"));
        issues.add(issue("Foo.java", 0, "INFO", "invalid line"));

        List<GitHubClient.IssueAnnot> annotations =
            ResultFeedback.buildAnnotations(issues, diff);

        assertEquals(2, annotations.size());
        assertEquals("Foo.java", annotations.get(0).path());
        assertEquals(10, annotations.get(0).line());
        assertEquals("warning", annotations.get(0).annotationLevel());
        assertEquals(11, annotations.get(1).line());
        assertEquals("failure", annotations.get(1).annotationLevel());
    }

    private static JsonNode issue(String file, int line, String severity, String message)
            throws Exception {
        return MAPPER.readTree("""
            {"file":"%s","line":%d,"severity":"%s","message":"%s"}
            """.formatted(file, line, severity, message));
    }

    @Test
    void findsLineInSimpleHunk() {
        String diff = """
            diff --git a/Foo.java b/Foo.java
            index abc..def 100644
            --- a/Foo.java
            +++ b/Foo.java
            @@ -10,5 +10,7 @@
              unchanged 1
              unchanged 2
            + added line
            + another added
              unchanged 3
            """;
        int result = ResultFeedback.mapToDiffLine(diff, "Foo.java", 12);
        assertTrue(result > 0, "Should find line 12 in diff, got: " + result);
    }

    @Test
    void returnsNegativeWhenLineNotFound() {
        String diff = """
            diff --git a/Foo.java b/Foo.java
            --- a/Foo.java
            +++ b/Foo.java
            @@ -10,3 +10,4 @@
              line10
              line11
            + added
            """;
        assertEquals(-1, ResultFeedback.mapToDiffLine(diff, "Foo.java", 99));
    }

    @Test
    void emptyDiffReturnsNegative() {
        assertEquals(-1, ResultFeedback.mapToDiffLine(null, "Foo.java", 10));
        assertEquals(-1, ResultFeedback.mapToDiffLine("", "Foo.java", 10));
    }

    @Test
    void multiHunkMapping() {
        String diff = """
            diff --git a/Foo.java b/Foo.java
            --- a/Foo.java
            +++ b/Foo.java
            @@ -10,5 +10,7 @@
              ctx10
              ctx11
            + added12
            + added13
              ctx14
            @@ -30,3 +35,4 @@
              ctx35
              ctx36
            + added37
            """;
        assertEquals(12, ResultFeedback.mapToDiffLine(diff, "Foo.java", 12));
        assertEquals(13, ResultFeedback.mapToDiffLine(diff, "Foo.java", 13));
        assertEquals(37, ResultFeedback.mapToDiffLine(diff, "Foo.java", 37));
    }

    @Test
    void fileNotFoundInDiff() {
        String diff = """
            diff --git a/Bar.java b/Bar.java
            --- a/Bar.java
            +++ b/Bar.java
            @@ -1,1 +1,2 @@
            + new
            """;
        assertEquals(-1, ResultFeedback.mapToDiffLine(diff, "Foo.java", 10));
    }

    @Test
    void filePathMustNotPrefixMatchAnotherFile() {
        String diff = """
            diff --git a/Foo.java.bak b/Foo.java.bak
            --- a/Foo.java.bak
            +++ b/Foo.java.bak
            @@ -9,1 +10,2 @@
              context
            + added
            """;

        assertEquals(-1, ResultFeedback.mapToDiffLine(diff, "Foo.java", 10));
    }

    @Test
    void renameUsesNewSideFilePath() {
        String diff = """
            diff --git a/OldName.java b/NewName.java
            similarity index 80%
            rename from OldName.java
            rename to NewName.java
            --- a/OldName.java
            +++ b/NewName.java
            @@ -10,1 +10,2 @@
              context
            + added
            """;

        assertEquals(11, ResultFeedback.mapToDiffLine(diff, "NewName.java", 11));
        assertEquals(-1, ResultFeedback.mapToDiffLine(diff, "OldName.java", 11));
    }

    @Test
    void skipsDeletedLines() {
        String diff = """
            diff --git a/Foo.java b/Foo.java
            --- a/Foo.java
            +++ b/Foo.java
            @@ -10,5 +10,4 @@
              ctx10
            - deleted11
              ctx12
            + added13
              ctx14
            """;
        assertEquals(12, ResultFeedback.mapToDiffLine(diff, "Foo.java", 12));
        assertEquals(13, ResultFeedback.mapToDiffLine(diff, "Foo.java", 13));
    }
}
