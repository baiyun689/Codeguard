package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.ProjectKey;
import com.codeguard.agent.graph.ProjectSnapshot;
import com.codeguard.agent.graph.ProjectSnapshotManager;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;
import java.util.concurrent.CompletableFuture;

import com.fasterxml.jackson.databind.JsonNode;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class GraphToolsTest {

    @Test
    void resolvesChangedLineAndUsesSymbolForImpactQuery(@TempDir Path repo) throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("Service.java"), """
                package demo;
                class Service { void run() {} }
                """);
        Files.writeString(root.resolve("Caller.java"), """
                package demo;
                class Caller { void call(Service service) { service.run(); } }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "rev"));
        AgentContext context = new AgentContext(repo, Set.of("src/main/java/demo/Service.java"));

        ToolResult resolved = new ResolveChangeContextTool(snapshot).execute(
                """
                {"changes":[{"file":"src/main/java/demo/Service.java","lines":[2]}]}
                """, context);
        assertTrue(resolved.isSuccess(), resolved.getError());
        assertTrue(resolved.getResult().contains("\"symbol_id\":\"java:demo.Service#run()\""),
                resolved.getResult());

        ToolResult impact = new InspectChangeImpactTool(snapshot)
                .execute("java:demo.Service#run()", context);
        assertTrue(impact.isSuccess(), impact.getError());
        assertTrue(impact.getResult().contains("Caller.java"), impact.getResult());
        assertTrue(impact.getResult().contains("\"status\":\"confirmed\""), impact.getResult());
    }

    @Test
    void fileReaderRejectsPathsNotGroundedInSnapshotOrTask(@TempDir Path repo) throws Exception {
        Files.writeString(repo.resolve("Known.java"), "class Known {}");
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "rev"));
        GetFileContentTool tool = new GetFileContentTool(
                new FileAccessSandbox(repo), snapshot);
        AgentContext context = new AgentContext(repo, Set.of());

        ToolResult missing = tool.execute("GuessedController.java", context);

        assertFalse(missing.isSuccess());
        assertTrue(missing.getError().contains("unconfirmed_path"), missing.getError());
    }

    @Test
    void testOnlyCallerIsSeparatedFromProductionImpact(@TempDir Path repo) throws Exception {
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
                class ServiceTest { void verifies(Service service) { service.run(); } }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "test-caller"));
        AgentContext context =
                new AgentContext(repo, Set.of("src/main/java/demo/Service.java"));

        ToolResult impact = new InspectChangeImpactTool(snapshot)
                .execute("java:demo.Service#run()", context);
        JsonNode payload = GraphToolSupport.JSON.readTree(impact.getResult());

        assertTrue(impact.isSuccess(), impact.getError());
        assertTrue(payload.path("relationships").isEmpty(), impact.getResult());
        assertFalse(payload.path("test_relationships").isEmpty(), impact.getResult());
        assertTrue(payload.path("test_relationships").toString()
                .contains("ServiceTest.java"), impact.getResult());
        assertTrue(payload.path("test_relationships").get(0)
                .path("source_set").asText().equals("TEST"), impact.getResult());
        assertTrue(payload.path("status").asText().equals("not_found"), impact.getResult());
        assertTrue(payload.path("source_scope").asText().equals("MAIN"), impact.getResult());
    }

    @Test
    void testSubjectUsesTestRelationshipsAsPrimaryEvidence(@TempDir Path repo)
            throws Exception {
        Path testRoot = repo.resolve("src/test/java/demo");
        Files.createDirectories(testRoot);
        Files.writeString(testRoot.resolve("ServiceTest.java"), """
                package demo;
                class ServiceTest {
                    void helper() {}
                    void verifies() { helper(); }
                }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "test-subject"));
        AgentContext context =
                new AgentContext(repo, Set.of("src/test/java/demo/ServiceTest.java"));

        ToolResult impact = new InspectChangeImpactTool(snapshot)
                .execute("java:demo.ServiceTest#helper()", context);
        JsonNode payload = GraphToolSupport.JSON.readTree(impact.getResult());

        assertTrue(payload.path("status").asText().equals("confirmed"), impact.getResult());
        assertTrue(payload.path("source_scope").asText().equals("TEST"), impact.getResult());
        assertFalse(payload.path("relationships").isEmpty(), impact.getResult());
        assertFalse(payload.path("test_relationships").isEmpty(), impact.getResult());
    }

    @Test
    void fieldSymbolReturnsReadWriteReferences(@TempDir Path repo) throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("State.java"), """
                package demo;
                class State {
                    int counter;
                    void reset() { counter = 0; }
                    int read() { return counter; }
                }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "field-rev"));
        AgentContext context = new AgentContext(repo, Set.of("src/main/java/demo/State.java"));

        ToolResult impact = new InspectChangeImpactTool(snapshot)
                .execute("java:demo.State#counter", context);
        assertTrue(impact.isSuccess(), impact.getError());
        assertTrue(impact.getResult().contains("\"kind\":\"READS_FIELD\"")
                        && impact.getResult().contains("\"kind\":\"WRITES_FIELD\""),
                impact.getResult());
        assertTrue(impact.getResult().contains("reset()"), impact.getResult());
        assertTrue(impact.getResult().contains("read()"), impact.getResult());
    }

    @Test
    void typeSymbolReturnsExtendsAndImplements(@TempDir Path repo) throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("Base.java"), """
                package demo;
                interface Base { void run(); }
                """);
        Files.writeString(root.resolve("Impl.java"), """
                package demo;
                class Impl implements Base { public void run() {} }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "type-rev"));
        AgentContext context = new AgentContext(repo, Set.of("src/main/java/demo/Base.java"));

        ToolResult impact = new InspectChangeImpactTool(snapshot)
                .execute("java:demo.Base", context);
        assertTrue(impact.isSuccess(), impact.getError());
        assertTrue(impact.getResult().contains("\"kind\":\"IMPLEMENTS\""), impact.getResult());
        assertTrue(impact.getResult().contains("Impl.java"), impact.getResult());
    }

    @Test
    void fieldSymbolSecurityPathReturnsReadersWritersAndSensitiveType(
            @TempDir Path repo
    ) throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("State.java"), """
                package demo;
                import java.util.concurrent.ExecutorService;
                class State {
                    ExecutorService executor;
                    void init() { executor = java.util.concurrent.Executors.newFixedThreadPool(1); }
                    void run() { executor.execute(() -> {}); }
                }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "field-sec"));
        AgentContext context = new AgentContext(repo, Set.of("src/main/java/demo/State.java"));

        ToolResult impact = new InspectSecurityPathTool(snapshot)
                .execute("java:demo.State#executor", context);

        assertTrue(impact.isSuccess(), impact.getError());
        assertTrue(impact.getResult().contains("\"kind\":\"READS_FIELD\"")
                        && impact.getResult().contains("\"kind\":\"WRITES_FIELD\""),
                impact.getResult());
        assertTrue(impact.getResult().contains("field_type_sensitive"), impact.getResult());
        assertTrue(impact.getResult().contains("ExecutorService"), impact.getResult());
    }

    @Test
    void typeSymbolSecurityPathReturnsInternalSensitiveCallsAndInheritors(
            @TempDir Path repo
    ) throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("Base.java"), """
                package demo;
                class Base {
                    void run() { Runtime.getRuntime().exec("ls"); }
                }
                """);
        Files.writeString(root.resolve("Impl.java"), """
                package demo;
                class Impl extends Base { }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "type-sec"));
        AgentContext context = new AgentContext(repo, Set.of("src/main/java/demo/Base.java"));

        ToolResult impact = new InspectSecurityPathTool(snapshot)
                .execute("java:demo.Base", context);

        assertTrue(impact.isSuccess(), impact.getError());
        assertTrue(impact.getResult().contains("\"kind\":\"EXTENDS\""), impact.getResult());
        assertTrue(impact.getResult().contains("\"kind\":\"CALLS\""), impact.getResult());
        assertTrue(impact.getResult().contains("exec"), impact.getResult());
    }

    @Test
    void unresolvedProjectNeverReportsConfirmedAbsence(@TempDir Path repo) throws Exception {
        Files.writeString(repo.resolve("Partial.java"), """
                class Partial {
                    void run() { unknownTarget.execute(); }
                }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "partial"));
        AgentContext context = new AgentContext(repo, Set.of("Partial.java"));

        ToolResult impact = new InspectChangeImpactTool(snapshot)
                .execute("java:Partial#missing()", context);
        ToolResult contextResult = new ResolveChangeContextTool(snapshot).execute(
                """
                {"changes":[{"file":"Partial.java","lines":[2]}]}
                """, context);

        assertTrue(impact.isSuccess(), impact.getError());
        assertTrue(impact.getResult().contains("\"status\":\"unknown\""), impact.getResult());
        assertTrue(impact.getResult().contains("\"coverage\":\"partial\""), impact.getResult());
        assertTrue(contextResult.getResult().contains("\"status\":\"confirmed\""),
                contextResult.getResult());
    }

    @Test
    void truncatedGraphResultIsPartialAndInsufficientForConfirmation(
            @TempDir Path repo
    ) throws Exception {
        StringBuilder source = new StringBuilder("""
                class LargeCaller {
                    void target() {}
                """);
        for (int index = 0; index < 201; index++) {
            source.append("    void caller").append(index).append("() { target(); }\n");
        }
        source.append("}\n");
        Files.writeString(repo.resolve("LargeCaller.java"), source);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "bounded"));
        AgentContext context = new AgentContext(repo, Set.of("LargeCaller.java"));

        ToolResult result = new InspectChangeImpactTool(snapshot)
                .execute("java:LargeCaller#target()", context);

        assertTrue(result.getResult().contains("\"status\":\"confirmed\""), result.getResult());
        assertTrue(result.getResult().contains("\"coverage\":\"partial\""), result.getResult());
        assertTrue(result.getResult().contains("result_truncated"), result.getResult());
    }
}
