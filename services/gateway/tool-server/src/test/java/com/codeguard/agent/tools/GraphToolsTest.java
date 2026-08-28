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

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class GraphToolsTest {

    @Test
    void behaviorPathReturnsDownstreamCalls(@TempDir Path repo) throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("Service.java"), """
                package demo;
                class Service { void run() { helper(); } void helper() {} }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "behavior-path"));
        AgentContext context = new AgentContext(repo, Set.of("src/main/java/demo/Service.java"));

        ToolResult result = new InspectPathTool(snapshot)
                .execute("{\"symbol_id\":\"java:demo.Service#run()\",\"path_kind\":\"behavior\"}", context);
        JsonNode payload = GraphToolSupport.JSON.readTree(result.getResult());

        assertTrue(result.isSuccess(), result.getError());
        assertEquals("behavior", payload.path("path_kind").asText(), result.getResult());
        assertTrue(payload.path("relationships").toString().contains("helper()"), result.getResult());
        assertTrue(payload.path("outcome").asText().equals("found"), result.getResult());
    }

    @Test
    void pathRejectsUnknownKind(@TempDir Path repo) throws Exception {
        Files.writeString(repo.resolve("Service.java"), "class Service { void run() {} }");
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "invalid-path-kind"));
        AgentContext context = new AgentContext(repo, Set.of("Service.java"));

        ToolResult result = new InspectPathTool(snapshot)
                .execute("{\"symbol_id\":\"java:Service#run()\",\"path_kind\":\"other\"}", context);

        assertFalse(result.isSuccess());
        assertEquals("invalid_path_kind", result.getError());
    }

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
        assertTrue(impact.getResult().contains("\"schema_version\":2"), impact.getResult());
        assertTrue(impact.getResult().contains("\"outcome\":\"found\""), impact.getResult());
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
        assertEquals("missing_file", missing.getError());
    }

    @Test
    void testOnlyCallerIsExcludedFromMainCanonicalResult(@TempDir Path repo) throws Exception {
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
        assertFalse(payload.has("main_symbols"), impact.getResult());
        assertFalse(payload.has("test_symbols"), impact.getResult());
        assertFalse(payload.has("generated_symbols"), impact.getResult());
        assertFalse(payload.has("main_relationships"), impact.getResult());
        assertFalse(payload.has("test_relationships"), impact.getResult());
        assertFalse(payload.has("generated_relationships"), impact.getResult());
        assertTrue(payload.path("outcome").asText().equals("not_found"), impact.getResult());
        assertTrue(payload.path("coverage").asText().equals("complete"), impact.getResult());
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

        assertTrue(payload.path("outcome").asText().equals("found"), impact.getResult());
        assertTrue(payload.path("source_scope").asText().equals("TEST"), impact.getResult());
        assertFalse(payload.path("relationships").isEmpty(), impact.getResult());
        assertTrue(payload.path("relationships").get(0)
                .path("source_set").asText().equals("TEST"), impact.getResult());
        assertFalse(payload.has("test_relationships"), impact.getResult());
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

        ToolResult impact = new InspectPathTool(snapshot)
                .execute("{\"symbol_id\":\"java:demo.State#executor\",\"path_kind\":\"security\"}", context);

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

        ToolResult impact = new InspectPathTool(snapshot)
                .execute("{\"symbol_id\":\"java:demo.Base\",\"path_kind\":\"security\"}", context);

        assertTrue(impact.isSuccess(), impact.getError());
        assertTrue(impact.getResult().contains("\"kind\":\"EXTENDS\""), impact.getResult());
        assertTrue(impact.getResult().contains("\"kind\":\"CALLS\""), impact.getResult());
        assertTrue(impact.getResult().contains("exec"), impact.getResult());
    }

    @Test
    void missingSubjectProducesIndeterminateQuery(@TempDir Path repo) throws Exception {
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
        assertTrue(impact.getResult().contains("\"outcome\":\"indeterminate\""), impact.getResult());
        assertTrue(impact.getResult().contains("\"coverage\":\"partial\""), impact.getResult());
        assertTrue(contextResult.getResult().contains("\"outcome\":\"found\""),
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

        assertTrue(result.getResult().contains("\"outcome\":\"found\""), result.getResult());
        assertTrue(result.getResult().contains("\"coverage\":\"partial\""), result.getResult());
        assertTrue(result.getResult().contains("result_truncated"), result.getResult());
    }

    @Test
    void unrelatedUnresolvedEdgeDoesNotPoisonResolvedQuery(@TempDir Path repo)
            throws Exception {
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
        Files.writeString(root.resolve("Unrelated.java"), """
                package demo;
                class Unrelated { void broken() { missing.call(); } }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "query-coverage"));
        AgentContext context = new AgentContext(
                repo, Set.of("src/main/java/demo/Service.java"));

        ToolResult result = new InspectChangeImpactTool(snapshot)
                .execute("java:demo.Service#run()", context);
        JsonNode payload = GraphToolSupport.JSON.readTree(result.getResult());

        assertTrue(payload.path("outcome").asText().equals("found"), result.getResult());
        assertTrue(payload.path("coverage").asText().equals("complete"), result.getResult());
        assertTrue(payload.path("snapshot_main_coverage").asText().equals("partial"),
                result.getResult());
    }

    @Test
    void mixedRelationsKeepResolvedFactsAndReportPartialCoverage(@TempDir Path repo)
            throws Exception {
        Files.writeString(repo.resolve("Mixed.java"), """
                class Mixed {
                    void run() throws Exception {
                        Runtime.getRuntime().exec("ls");
                        missing.execute();
                    }
                }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "mixed-relations"));
        AgentContext context = new AgentContext(repo, Set.of("Mixed.java"));

        ToolResult result = new InspectPathTool(snapshot)
                .execute("{\"symbol_id\":\"java:Mixed#run()\",\"path_kind\":\"security\"}", context);
        JsonNode payload = GraphToolSupport.JSON.readTree(result.getResult());

        assertTrue(payload.path("outcome").asText().equals("found"), result.getResult());
        assertTrue(payload.path("coverage").asText().equals("partial"), result.getResult());
        assertFalse(payload.path("relationships").isEmpty(), result.getResult());
        assertFalse(payload.path("unresolved_relationships").isEmpty(), result.getResult());
        assertTrue(payload.path("unresolved_count").asInt() > 0, result.getResult());
    }

    @Test
    void ordinaryUnresolvedCallIsAggregatedAsSecurityCoverageGap(@TempDir Path repo)
            throws Exception {
        Files.writeString(repo.resolve("Ordinary.java"), """
                class Ordinary {
                    void run() { missing.refresh(); }
                }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "ordinary-unresolved-security"));
        AgentContext context = new AgentContext(repo, Set.of("Ordinary.java"));

        ToolResult result = new InspectPathTool(snapshot)
                .execute("{\"symbol_id\":\"java:Ordinary#run()\",\"path_kind\":\"security\"}", context);
        JsonNode payload = GraphToolSupport.JSON.readTree(result.getResult());

        assertEquals("indeterminate", payload.path("outcome").asText(), result.getResult());
        assertEquals("partial", payload.path("coverage").asText(), result.getResult());
        assertEquals(1, payload.path("unresolved_count").asInt(), result.getResult());
        assertTrue(payload.path("unresolved_relationships").isEmpty(), result.getResult());
        assertTrue(result.getResult().contains("unresolved_relationships_suppressed:1"),
                result.getResult());
    }

    @Test
    void sensitiveUnresolvedCallStillLimitsSecurityQuery(@TempDir Path repo)
            throws Exception {
        Files.writeString(repo.resolve("Sensitive.java"), """
                class Sensitive {
                    void run() { missing.execute(); }
                }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "sensitive-unresolved-security"));
        AgentContext context = new AgentContext(repo, Set.of("Sensitive.java"));

        ToolResult result = new InspectPathTool(snapshot)
                .execute("{\"symbol_id\":\"java:Sensitive#run()\",\"path_kind\":\"security\"}", context);
        JsonNode payload = GraphToolSupport.JSON.readTree(result.getResult());

        assertEquals("indeterminate", payload.path("outcome").asText(), result.getResult());
        assertEquals("partial", payload.path("coverage").asText(), result.getResult());
        assertEquals(1, payload.path("unresolved_count").asInt(), result.getResult());
        assertFalse(payload.path("unresolved_relationships").isEmpty(), result.getResult());
    }

    @Test
    void resolvedCallChainStillFindsNestedSensitiveSink(@TempDir Path repo)
            throws Exception {
        Files.writeString(repo.resolve("ResolvedChain.java"), """
                class ResolvedChain {
                    void run() throws Exception { helper(); }
                    void helper() throws Exception { Runtime.getRuntime().exec("ls"); }
                }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "resolved-security-chain"));
        AgentContext context = new AgentContext(repo, Set.of("ResolvedChain.java"));

        ToolResult result = new InspectPathTool(snapshot)
                .execute("{\"symbol_id\":\"java:ResolvedChain#run()\",\"path_kind\":\"security\"}", context);
        JsonNode payload = GraphToolSupport.JSON.readTree(result.getResult());

        assertEquals("found", payload.path("outcome").asText(), result.getResult());
        assertFalse(payload.path("relationships").isEmpty(), result.getResult());
        assertTrue(result.getResult().contains("exec"), result.getResult());
    }

    @Test
    void resolvedSecurityTraversalHonorsThreeLayerBoundary(@TempDir Path repo)
            throws Exception {
        Files.writeString(repo.resolve("DepthBoundary.java"), """
                class DepthBoundary {
                    void within() throws Exception { withinOne(); }
                    void withinOne() throws Exception { withinTwo(); }
                    void withinTwo() throws Exception { Runtime.getRuntime().exec("ls"); }

                    void beyond() throws Exception { beyondOne(); }
                    void beyondOne() throws Exception { beyondTwo(); }
                    void beyondTwo() throws Exception { beyondThree(); }
                    void beyondThree() throws Exception { Runtime.getRuntime().exec("ls"); }
                }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "security-depth-boundary"));
        AgentContext context = new AgentContext(repo, Set.of("DepthBoundary.java"));

        ToolResult within = new InspectPathTool(snapshot)
                .execute("{\"symbol_id\":\"java:DepthBoundary#within()\",\"path_kind\":\"security\"}", context);
        ToolResult beyond = new InspectPathTool(snapshot)
                .execute("{\"symbol_id\":\"java:DepthBoundary#beyond()\",\"path_kind\":\"security\"}", context);

        assertTrue(within.getResult().contains("\"outcome\":\"found\""),
                within.getResult());
        assertTrue(within.getResult().contains("exec"), within.getResult());
        assertTrue(beyond.getResult().contains("\"outcome\":\"not_found\""),
                beyond.getResult());
        assertFalse(beyond.getResult().contains("exec"), beyond.getResult());
    }

    @Test
    void potentialUnresolvedCallerPreventsConfirmedAbsence(@TempDir Path repo)
            throws Exception {
        Files.writeString(repo.resolve("Service.java"), """
                class Service { void run() {} }
                """);
        Files.writeString(repo.resolve("ExternalCaller.java"), """
                class ExternalCaller {
                    void call(MissingService service) { service.run(); }
                }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "potential-caller"));
        AgentContext context = new AgentContext(repo, Set.of("Service.java"));

        ToolResult result = new InspectChangeImpactTool(snapshot)
                .execute("java:Service#run()", context);
        JsonNode payload = GraphToolSupport.JSON.readTree(result.getResult());

        assertTrue(payload.path("outcome").asText().equals("indeterminate"),
                result.getResult());
        assertTrue(payload.path("coverage").asText().equals("partial"),
                result.getResult());
        assertTrue(payload.path("relationships").isEmpty(), result.getResult());
        assertFalse(payload.path("unresolved_relationships").isEmpty(),
                result.getResult());
    }
}
