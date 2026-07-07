package com.codeguard.ci.executor;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class ResultFeedbackLineMappingTest {

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
