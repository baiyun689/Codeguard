package com.codeguard.agent.graph;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class SourceSnapshotBuilderTest {

    @Test
    void buildsOnlyTheRequestedSourceFile(@TempDir Path repo) throws Exception {
        Path sourceRoot = repo.resolve("src/main/java/demo");
        Files.createDirectories(sourceRoot);
        Files.writeString(sourceRoot.resolve("Service.java"), """
                package demo;
                class Service { void run() { helper(); } void helper() {} }
                """);
        Files.writeString(sourceRoot.resolve("Unrelated.java"), """
                package demo;
                class Unrelated { void other() {} }
                """);

        ProjectSnapshot snapshot = SourceSnapshotBuilder.build(
                ProjectKey.of(repo, "source-only"),
                "{\"symbol_id\":\"java:demo.Service#run()\"}");

        assertEquals(1, snapshot.sources().size());
        assertEquals(1, snapshot.astUnits().size());
        assertTrue(snapshot.graph().node("java:demo.Service#run()").isPresent(),
                snapshot.graph().nodes().toString());
        assertTrue(snapshot.sources().containsKey("src/main/java/demo/Service.java"));
        assertFalse(snapshot.sources().containsKey("src/main/java/demo/Unrelated.java"));
    }
}
