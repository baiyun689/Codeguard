package com.codeguard.agent.graph;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

class ProjectSnapshotManagerTest {

    @Test
    void parsesModernJavaSyntaxWithoutDowngradingSnapshot(@TempDir Path repo) throws Exception {
        Files.writeString(repo.resolve("Modern.java"), """
                record Modern(int value) {
                    static int classify(Object input) {
                        if (input instanceof String text && !text.isBlank()) {
                            return switch (text.length()) {
                                case 1, 2 -> 1;
                                default -> 2;
                            };
                        }
                        return 0;
                    }
                }
                """);

        ProjectSnapshot snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "modern")).join();

        assertEquals(1, snapshot.astUnits().size());
        assertTrue(snapshot.diagnostics().isEmpty());
    }

    @Test
    void buildsOneCompleteSnapshotWithCallAndSpringEntrypoint(@TempDir Path repo) throws Exception {
        Path sourceRoot = repo.resolve("src/main/java/com/example");
        Files.createDirectories(sourceRoot);
        Files.writeString(sourceRoot.resolve("OrderService.java"), """
                package com.example;
                class OrderService {
                    void processOrder(String id) {}
                }
                """);
        Files.writeString(sourceRoot.resolve("OrderController.java"), """
                package com.example;
                import org.springframework.web.bind.annotation.GetMapping;
                class OrderController {
                    private final OrderService service = new OrderService();
                    @GetMapping("/orders")
                    void getOrder() {
                        service.processOrder("42");
                    }
                }
                """);

        ProjectSnapshotManager manager = new ProjectSnapshotManager();
        ProjectKey key = ProjectKey.of(repo, "abc123");
        ProjectSnapshot first = manager.getOrBuild(key).join();
        ProjectSnapshot second = manager.getOrBuild(key).join();

        assertSame(first, second);
        assertEquals(2, first.astUnits().size());
        String serviceId = first.graph()
                .symbolsInFile("src/main/java/com/example/OrderService.java")
                .stream()
                .filter(node -> node.kind() == GraphNodeKind.METHOD)
                .findFirst()
                .orElseThrow()
                .id();
        assertTrue(first.graph().incoming(serviceId, GraphEdgeKind.CALLS).stream()
                .anyMatch(edge -> edge.file().endsWith("OrderController.java")));
        assertTrue(first.graph().nodes().stream()
                .anyMatch(node -> node.kind() == GraphNodeKind.FRAMEWORK_ENTRYPOINT
                        && node.signature().contains("/orders")));
    }

    @Test
    void unresolvedRelationshipsMakeCoveragePartial(@TempDir Path repo) throws Exception {
        Files.writeString(repo.resolve("Broken.java"), """
                class Broken {
                    void run() { missingDependency.execute(); }
                }
                """);

        ProjectSnapshot snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "partial")).join();

        assertFalse(snapshot.complete());
        assertEquals("partial", snapshot.coverageStatus());
        assertTrue(snapshot.graph().edges().stream()
                .anyMatch(edge -> edge.resolution() == ResolutionStatus.UNRESOLVED));
    }

    @Test
    void testResolutionFailuresDoNotDowngradeProductionCoverage(
            @TempDir Path repo
    ) throws Exception {
        Path mainRoot = repo.resolve("src/main/java/demo");
        Path testRoot = repo.resolve("src/test/java/demo");
        Files.createDirectories(mainRoot);
        Files.createDirectories(testRoot);
        Files.writeString(mainRoot.resolve("Service.java"), """
                package demo;
                class Service { void run() {} }
                """);
        Files.writeString(testRoot.resolve("ServiceTest.java"), """
                package demo;
                class ServiceTest {
                    void brokenTest() { missingTestDependency.execute(); }
                }
                """);

        ProjectSnapshot snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "source-sets")).join();

        assertTrue(snapshot.productionComplete());
        assertFalse(snapshot.testComplete());
        assertEquals("complete", snapshot.coverageStatus(SourceSet.MAIN));
        assertEquals("partial", snapshot.coverageStatus(SourceSet.TEST));
        assertTrue(snapshot.graph().symbolsInFile(
                        "src/test/java/demo/ServiceTest.java").stream()
                .allMatch(node -> node.sourceSet() == SourceSet.TEST));
        assertTrue(snapshot.graph().edges().stream()
                .filter(edge -> edge.file().endsWith("ServiceTest.java"))
                .allMatch(edge -> edge.sourceSet() == SourceSet.TEST));
    }

    @Test
    void globalScanFailureDowngradesEverySourceSet(@TempDir Path repo) {
        ProjectSnapshot snapshot = new ProjectSnapshot(
                ProjectKey.of(repo, "scan-failed"),
                Map.of(),
                Map.of(),
                new ProjectCodeGraph(List.of(), List.of()),
                List.of("scan_failed: access denied"));

        assertEquals("partial", snapshot.coverageStatus(SourceSet.MAIN));
        assertEquals("partial", snapshot.coverageStatus(SourceSet.TEST));
        assertEquals("partial", snapshot.coverageStatus(SourceSet.GENERATED));
    }

    @Test
    void rejectsJavaSymlinksInsteadOfReadingOutsideRepository(@TempDir Path repo) throws Exception {
        Path outside = Files.createTempFile("codeguard-outside-", ".java");
        Files.writeString(outside, "class SecretOutside {}");
        Path link = repo.resolve("Leak.java");
        try {
            Files.createSymbolicLink(link, outside);
        } catch (Exception unsupported) {
            assumeTrue(false, "symbolic links are unavailable: " + unsupported.getMessage());
        }

        ProjectSnapshot snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "symlink")).join();

        assertFalse(snapshot.sources().containsKey("Leak.java"));
        assertTrue(snapshot.diagnostics().stream()
                .anyMatch(diagnostic -> diagnostic.contains("symlink_rejected: Leak.java")));
    }
}
