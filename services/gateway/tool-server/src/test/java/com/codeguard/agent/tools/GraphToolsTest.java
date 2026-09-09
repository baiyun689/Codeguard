package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.ProjectKey;
import com.codeguard.agent.graph.ProjectSnapshot;
import com.codeguard.agent.graph.ProjectSnapshotManager;
import com.codeguard.agent.graph.GraphEdgeKind;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.CompletableFuture;

import com.fasterxml.jackson.databind.JsonNode;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

class GraphToolsTest {

    @Test
    void lazyChangeContextResolvesDirectTargetsOnlyOnChangedLines(@TempDir Path repo) throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("A.java"), """
                package demo;
                class A {
                    void run(B b) {
                        b.open();
                        b.other();
                    }
                }
                """);
        Files.writeString(root.resolve("B.java"),
                "package demo; class B { void open() {} void other() {} }");
        var provider = new ProjectSnapshotManager().lazyProvider(ProjectKey.of(repo, "changed-targets"));
        var result = new ResolveChangeContextTool(provider).execute("""
                {"changes":[{"file":"src/main/java/demo/A.java","lines":[4]}]}
                """, new AgentContext(repo));
        assertTrue(result.isSuccess(), result.getError());
        JsonNode refs = GraphToolSupport.JSON.readTree(result.getResult()).path("contexts").get(0).path("references");
        assertTrue(refs.toString().contains("java:demo.B#open()"), result.getResult());
        assertFalse(refs.toString().contains("#other()"), result.getResult());
        for (JsonNode ref : refs) {
            assertEquals(4, ref.path("line").asInt());
            assertEquals("resolved", ref.path("resolution").asText());
        }
    }

    @Test
    void callerExcerptCoversTheCallsiteAndExcludesTestSources(@TempDir Path repo) throws Exception {
        Path main = repo.resolve("src/main/java/demo");
        Path test = repo.resolve("src/test/java/demo");
        Files.createDirectories(main);
        Files.createDirectories(test);
        Files.writeString(main.resolve("Service.java"), """
                package demo;
                class Service { void save() {} }
                """);
        Files.writeString(main.resolve("Caller.java"), "package demo;\nclass Caller {\n void run(Service s) {\n"
                + " // padding\n".repeat(60) + " s.save();\n }\n}\n");
        Files.writeString(test.resolve("TestCaller.java"),
                "package demo; class TestCaller { void run(Service s) { s.save(); } }");
        var provider = new ProjectSnapshotManager().lazyProvider(ProjectKey.of(repo, "caller-source"));
        var result = new QueryRelationsTool(provider).execute("""
                {"subject_symbol_id":"java:demo.Service#save()","relation":"callers"}
                """, new AgentContext(repo));
        assertTrue(result.isSuccess(), result.getError());
        JsonNode page = GraphToolSupport.JSON.readTree(result.getResult());
        boolean found = false;
        for (JsonNode symbol : page.path("symbols")) {
            assertFalse(symbol.path("file").asText().contains("src/test/"));
            if (symbol.has("source_excerpt")) {
                found = true;
                assertEquals("java:demo.Caller#run(Service)", symbol.path("id").asText());
                assertTrue(symbol.path("source_excerpt").path("text").asText().contains("s.save();"));
                assertTrue(symbol.path("source_excerpt").path("truncated").asBoolean());
            }
        }
        assertTrue(found);
    }

    @Test
    void relationEndpointsIncludeBoundedSourceFromTheReturnedPage(@TempDir Path repo) throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("Service.java"), "package demo;\nclass Service {\n"
                + " void run() { a(); b(); c(); d(); }\n"
                + " void a() {\n" + "  int v = 0;\n".repeat(40) + " }\n"
                + " void b() {}\n void c() {}\n void d() {}\n}\n");
        var provider = new ProjectSnapshotManager().lazyProvider(ProjectKey.of(repo, "endpoint-source"));
        var tool = new QueryRelationsTool(provider);
        var context = new AgentContext(repo);
        var result = tool.execute("""
                {"subject_symbol_id":"java:demo.Service#run()","relation":"callees","limit":1}
                """, context);
        assertTrue(result.isSuccess(), result.getError());
        JsonNode page = GraphToolSupport.JSON.readTree(result.getResult());
        String target = page.path("relationships").get(0).path("targetId").asText();
        int snippets = 0;
        for (JsonNode symbol : page.path("symbols")) {
            if (symbol.has("source_excerpt")) {
                snippets++;
                assertEquals(target, symbol.path("id").asText());
                JsonNode excerpt = symbol.path("source_excerpt");
                assertTrue(excerpt.path("text").asText().contains("void a()"));
                assertTrue(excerpt.path("text").asText().length() <= 1000);
                assertTrue(excerpt.path("truncated").asBoolean());
                assertTrue(excerpt.path("end_line").asInt() - excerpt.path("start_line").asInt() < 24);
                assertEquals(excerpt.path("end_line").asInt() + 1, excerpt.path("next_cursor").asInt());
            }
        }
        assertEquals(1, snippets);
        var all = GraphToolSupport.JSON.readTree(tool.execute("""
                {"subject_symbol_id":"java:demo.Service#run()","relation":"callees"}
                """, context).getResult());
        int count = 0;
        for (JsonNode symbol : all.path("symbols")) {
            if (symbol.has("source_excerpt")) count++;
        }
        assertEquals(3, count);
        assertEquals(1, all.path("omitted_source_excerpt_count").asInt());
        var noContext = GraphToolSupport.JSON.readTree(tool.execute("""
                {"subject_symbol_id":"java:demo.Service#run()","relation":"callees","include_context":false}
                """, context).getResult());
        for (JsonNode symbol : noContext.path("symbols")) assertFalse(symbol.has("source_excerpt"));
    }

    @Test
    void largeTypeReturnsBoundedSourceAndNavigableMembers(@TempDir Path repo) throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("State.java"), "package demo;\nclass State {\n int value;\n"
                + " // padding\n".repeat(260) + " int read() { return value; }\n}\n");
        var provider = new ProjectSnapshotManager().lazyProvider(ProjectKey.of(repo, "member-pages"));
        var tool = new ReadSymbolTool(provider);
        var context = new AgentContext(repo);
        var first = tool.execute("{\"symbol_id\":\"java:demo.State\"}", context);
        assertTrue(first.isSuccess(), first.getError());
        assertTrue(first.getResult().contains("truncated: true"));
        String directory = first.getResult().lines().filter(line -> line.startsWith("members: "))
                .findFirst().orElseThrow().substring("members: ".length());
        JsonNode members = GraphToolSupport.JSON.readTree(directory);
        String fieldId = members.get(0).path("id").asText();
        assertEquals("FIELD", members.get(0).path("kind").asText());
        assertTrue(tool.execute("{\"symbol_id\":\"" + fieldId + "\"}", context)
                .getResult().contains("int value;"));
        int cursor = Integer.parseInt(first.getResult().lines()
                .filter(line -> line.startsWith("next_cursor: ")).findFirst().orElseThrow()
                .substring("next_cursor: ".length()));
        var next = tool.execute("{\"symbol_id\":\"java:demo.State\",\"cursor\":" + cursor + "}", context);
        assertTrue(next.isSuccess(), next.getError());
        assertTrue(next.getResult().contains("java:demo.State#read()"));
        assertTrue(next.getResult().contains("truncated: true"));
        assertFalse(next.getResult().contains("next_cursor:"));
    }

    @Test
    void lazyInterfaceNavigationPreservesDispatchFactsAcrossFiles(@TempDir Path repo) throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("Policy.java"), "package demo; interface Policy { boolean retry(); }");
        Files.writeString(root.resolve("Impl.java"),
                "package demo; class Impl implements Policy { public boolean retry() { return true; } }");
        Files.writeString(root.resolve("Consumer.java"),
                "package demo; class Consumer { boolean run(Policy p) { return p.retry(); } }");
        var provider = new ProjectSnapshotManager().lazyProvider(ProjectKey.of(repo, "interface-chain"));
        var tool = new QueryRelationsTool(provider);
        var context = new AgentContext(repo);
        var override = tool.execute("{\"subject_symbol_id\":\"java:demo.Impl#retry()\",\"relation\":\"overrides\"}", context);
        assertTrue(override.isSuccess(), override.getError());
        assertTrue(GraphToolSupport.JSON.readTree(override.getResult()).path("relationships")
                .toString().contains("java:demo.Policy#retry()"), override.getResult());
        var callers = tool.execute("{\"subject_symbol_id\":\"java:demo.Policy#retry()\",\"relation\":\"callers\"}", context);
        assertTrue(callers.isSuccess(), callers.getError());
        String edges = GraphToolSupport.JSON.readTree(callers.getResult()).path("relationships").toString();
        assertTrue(edges.contains("Consumer#run(Policy)"), edges);
        assertFalse(edges.contains("Impl#retry"), "Interface dispatch must not invent a direct implementation call");
        var implementations = tool.execute("{\"subject_symbol_id\":\"java:demo.Policy#retry()\",\"relation\":\"overrides\"}", context);
        assertTrue(GraphToolSupport.JSON.readTree(implementations.getResult()).path("relationships")
                .toString().contains("java:demo.Impl#retry()"), implementations.getResult());
    }

    @Test
    void lazyRelationQueryFindsIncomingAndOutgoingCalls(@TempDir Path repo) throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("Service.java"), """
                package demo;
                class Service {
                    void execute() { open(1); }
                    void open(int state) { helper(); }
                    void helper() {}
                }
                """);
        var provider = new ProjectSnapshotManager().lazyProvider(ProjectKey.of(repo, "lazy-relations"));
        var tool = new QueryRelationsTool(provider);
        for (String relation : new String[]{"callers", "callees"}) {
            ToolResult result = tool.execute("""
                    {"subject_symbol_id":"java:demo.Service#open(int)","relation":"%s"}
                    """.formatted(relation), new AgentContext(repo));
            assertTrue(result.isSuccess(), result.getError());
            JsonNode payload = GraphToolSupport.JSON.readTree(result.getResult());
            assertEquals("found", payload.path("outcome").asText(), result.getResult());
            assertTrue(payload.path("relationships").toString().contains(
                    relation.equals("callers") ? "execute()" : "helper()"), result.getResult());
        }
    }

    @Test
    void lazyFieldRelationsRequireAFieldAndReturnItsReadersAndWriters(@TempDir Path repo)
            throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("State.java"), """
                package demo;
                class State {
                    int value;
                    int read() { return value; }
                    void write() { value = 1; }
                }
                """);
        var provider = new ProjectSnapshotManager().lazyProvider(ProjectKey.of(repo, "lazy-fields"));
        var tool = new QueryRelationsTool(provider);
        for (String relation : new String[]{"field_readers", "field_writers"}) {
            ToolResult result = tool.execute("""
                    {"subject_symbol_id":"java:demo.State#value","relation":"%s"}
                    """.formatted(relation), new AgentContext(repo));
            assertTrue(result.isSuccess(), result.getError());
            assertEquals("found", GraphToolSupport.JSON.readTree(result.getResult())
                    .path("outcome").asText(), result.getResult());
        }
        ToolResult wrongSubject = tool.execute("""
                {"subject_symbol_id":"java:demo.State#read()","relation":"field_writers"}
                """, new AgentContext(repo));
        assertFalse(wrongSubject.isSuccess());
        assertTrue(wrongSubject.getError().startsWith("invalid_relation_subject:"));
    }



    @Test
    void unifiedRelationQuerySupportsTypedCalleesAndContinuation(@TempDir Path repo)
            throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("Service.java"), """
                package demo;
                class Service { void run() { helper(); } void helper() {} }
                """);
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "query-relations"));
        AgentContext context = new AgentContext(repo);

        ToolResult first = new QueryRelationsTool((toolName, input) ->
                GraphToolSupport.await(snapshot)).execute(
                "{\"subject_symbol_id\":\"java:demo.Service#run()\","
                        + "\"relation\":\"callees\",\"depth\":1,\"limit\":1}",
                context);
        JsonNode payload = GraphToolSupport.JSON.readTree(first.getResult());
        assertTrue(first.isSuccess(), first.getError());
        assertEquals("query_relations", payload.path("tool").asText());
        assertEquals("callees", payload.path("relation").asText());
        assertTrue(payload.path("relationships").toString().contains("helper()"),
                first.getResult());
        assertTrue(payload.path("relationships").get(0).has("callsite"),
                first.getResult());
    }





    @Test
    void fileReaderRejectsUnresolvedSymbol(@TempDir Path repo) throws Exception {
        Files.writeString(repo.resolve("Known.java"), "class Known {}");
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, "rev"));
        SymbolSourceReader tool = new SymbolSourceReader(snapshot);
        AgentContext context = new AgentContext(repo);

        ToolResult missing = tool.execute("GuessedController.java", context);

        assertFalse(missing.isSuccess());
        assertEquals("缺少 symbol_id", missing.getError());
    }































    @Test
    void lazyProviderBuildsIndexWithoutSemanticEdgesAndExpandsRequestedPath(
            @TempDir Path repo
    ) throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("Service.java"), """
                package demo;
                class Service { void run() { helper(); } void helper() {} }
                """);

        ProjectSnapshotManager manager = new ProjectSnapshotManager();
        ProjectSnapshot index = manager
                .getOrBuildIndex(ProjectKey.of(repo, "lazy-path"))
                .join();
        assertTrue(index.graph().edges().stream()
                .noneMatch(edge -> edge.kind() == GraphEdgeKind.CALLS));

        ProjectSnapshot expanded = manager.lazyProvider(ProjectKey.of(repo, "lazy-path"))
                .load("inspect_path", "{\"symbol_id\":\"java:demo.Service#run()\","
                        + "\"path_kind\":\"behavior\"}");
        assertTrue(expanded.graph().outgoing(
                        "java:demo.Service#run()", GraphEdgeKind.CALLS).stream()
                .anyMatch(edge -> edge.targetId().contains("helper()")));
    }

    @Test
    void lazyProviderResolvesReverseImpactWithoutBuildingWholeGraph(@TempDir Path repo)
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

        ProjectSnapshotManager manager = new ProjectSnapshotManager();
        ProjectSnapshot expanded = manager.lazyProvider(ProjectKey.of(repo, "lazy-impact"))
                .load("inspect_change_impact", "java:demo.Service#run()");
        assertTrue(expanded.graph().incoming(
                        "java:demo.Service#run()", GraphEdgeKind.CALLS).stream()
                .anyMatch(edge -> edge.file().endsWith("Caller.java")));
    }

    @Test
    void lazyProviderPreservesFieldAndTypeQueries(@TempDir Path repo) throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("Base.java"), """
                package demo;
                class Base {
                    int state;
                    void update() { state++; }
                    int read() { return state; }
                }
                """);
        Files.writeString(root.resolve("Impl.java"), """
                package demo;
                class Impl extends Base { }
                """);

        ProjectSnapshotManager manager = new ProjectSnapshotManager();
        ProjectSnapshot field = manager.lazyProvider(ProjectKey.of(repo, "lazy-field"))
                .load("inspect_change_impact", "java:demo.Base#state");
        assertTrue(field.graph().incoming(
                        "java:demo.Base#state", GraphEdgeKind.READS_FIELD).stream()
                .anyMatch(edge -> edge.file().endsWith("Base.java")), field.graph().edges().toString());

        ProjectSnapshot type = manager.lazyProvider(ProjectKey.of(repo, "lazy-type"))
                .load("inspect_change_impact", "java:demo.Base");
        assertTrue(type.graph().incoming(
                        "java:demo.Base", GraphEdgeKind.EXTENDS).stream()
                .anyMatch(edge -> edge.file().endsWith("Impl.java")));
    }

    @Test
    void lazyProviderKeepsUnresolvedCallerAsCoverageGap(@TempDir Path repo) throws Exception {
        Files.writeString(repo.resolve("Service.java"), "class Service { void run() {} }\n");
        Files.writeString(repo.resolve("ExternalCaller.java"), """
                class ExternalCaller {
                    void call(MissingService service) { service.run(); }
                }
                """);

        ProjectSnapshot expanded = new ProjectSnapshotManager()
                .lazyProvider(ProjectKey.of(repo, "lazy-potential-caller"))
                .load("inspect_change_impact", "java:Service#run()");
        assertTrue(expanded.graph().edges().stream()
                .anyMatch(edge -> edge.resolution() == com.codeguard.agent.graph.ResolutionStatus.UNRESOLVED
                        && edge.targetId().equals("unresolved:method:run/0")));
    }

    @Test
    void lazyStructureQueryRemainsOneHop(@TempDir Path repo) throws Exception {
        Files.writeString(repo.resolve("Chain.java"), """
                class Chain {
                    void run() { middle(); }
                    void middle() { leaf(); }
                    void leaf() { }
                }
                """);

        ProjectSnapshot expanded = new ProjectSnapshotManager()
                .lazyProvider(ProjectKey.of(repo, "lazy-structure"))
                .load("inspect_structure", "java:Chain#run()");
        assertTrue(expanded.graph().outgoing(
                        "java:Chain#run()", GraphEdgeKind.CALLS).stream()
                .anyMatch(edge -> edge.targetId().contains("middle()")));
        assertTrue(expanded.graph().outgoing(
                        "java:Chain#middle()", GraphEdgeKind.CALLS).isEmpty());
    }

    @Test
    void lazyStructureQueryKeepsResolvedIncomingCaller(@TempDir Path repo) throws Exception {
        Files.writeString(repo.resolve("Service.java"), """
                class Service { void run() { helper(); } void helper() {} }
                """);
        Files.writeString(repo.resolve("Caller.java"), """
                class Caller { void call(Service service) { service.run(); } }
                """);

        ProjectSnapshot expanded = new ProjectSnapshotManager()
                .lazyProvider(ProjectKey.of(repo, "lazy-structure-incoming"))
                .load("inspect_structure", "java:Service#run()");

        assertTrue(expanded.graph().incoming("java:Service#run()", GraphEdgeKind.CALLS)
                        .stream()
                        .anyMatch(edge -> edge.file().endsWith("Caller.java")),
                expanded.graph().edges().toString());
        assertTrue(expanded.graph().outgoing("java:Service#helper()", GraphEdgeKind.CALLS)
                        .isEmpty());
    }

    @Test
    void lazyProviderSingleFlightsAndCachesSuccessfulQuery(@TempDir Path repo)
            throws Exception {
        Files.writeString(repo.resolve("Service.java"), """
                class Service { void run() { helper(); } void helper() {} }
                """);

        ProjectSnapshotManager manager = new ProjectSnapshotManager();
        var provider = manager.lazyProvider(ProjectKey.of(repo, "lazy-cache"));
        String query = "{\"symbol_id\":\"java:Service#run()\","
                + "\"path_kind\":\"behavior\"}";

        ProjectSnapshot first = provider.load("inspect_path", query);
        ProjectSnapshot second = provider.load("inspect_path", query);

        assertSame(first, second);
    }




}
