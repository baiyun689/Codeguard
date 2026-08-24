package com.codeguard.ci.executor;

import com.codeguard.ci.github.GitHubClient;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;

class ResultFeedbackTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String DIFF = """
        diff --git a/src/App.java b/src/App.java
        --- a/src/App.java
        +++ b/src/App.java
        @@ -10,2 +10,3 @@
         context();
        +changed();
         tail();
        """;

    @Test
    void mapsOnlyAddedLinesForInlineFeedback() {
        assertEquals(-1, ResultFeedback.mapToDiffLine(DIFF, "src/App.java", 10));
        assertEquals(11, ResultFeedback.mapToDiffLine(DIFF, "src/App.java", 11));
        assertEquals(-1, ResultFeedback.mapToDiffLine(DIFF, "src/App.java", 12));
    }

    @Test
    void excludesFileLevelIssuesFromAnnotations() throws Exception {
        JsonNode issue = MAPPER.readTree("""
            {
              "severity": "WARNING",
              "file": "src/App.java",
              "line": 0,
              "message": "location unresolved"
            }
            """);

        List<GitHubClient.IssueAnnot> annotations =
            ResultFeedback.buildAnnotations(List.of(issue), DIFF);

        assertEquals(List.of(), annotations);
    }
}
