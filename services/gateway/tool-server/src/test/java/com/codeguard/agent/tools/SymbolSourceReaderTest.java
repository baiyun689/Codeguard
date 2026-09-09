package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.GraphNode;
import com.codeguard.agent.graph.GraphNodeKind;
import com.codeguard.agent.graph.ProjectKey;
import com.codeguard.agent.graph.ProjectSnapshot;
import com.codeguard.agent.graph.ProjectSnapshotManager;
import com.codeguard.agent.graph.ProjectSnapshotProvider;
import com.codeguard.agent.graph.SourceSnapshotProvider;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.CompletableFuture;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** read_symbol 的 symbol-only 契约测试。 */
class SymbolSourceReaderTest {

    private record Fixture(SymbolSourceReader tool, ProjectSnapshot snapshot, AgentContext context) {}

    private static String query(String symbolId) {
        return "{\"symbol_id\":\"" + symbolId + "\"}";
    }

    private Fixture fixture(Path repo, String revision) {
        CompletableFuture<ProjectSnapshot> snapshot = new ProjectSnapshotManager()
                .getOrBuild(ProjectKey.of(repo, revision));
        return new Fixture(
                new SymbolSourceReader(snapshot),
                snapshot.join(),
                new AgentContext(repo));
    }

    @Test
    void methodReturnsAnnotationsSignatureAndBody(@TempDir Path repo) throws Exception {
        Path file = repo.resolve("src/main/java/demo/Service.java");
        Files.createDirectories(file.getParent());
        Files.writeString(file, """
                package demo;
                class Service {
                    @Deprecated
                    private void run() { helper(); }
                    private void helper() {}
                }
                """);

        Fixture fixture = fixture(repo, "method-source");
        ToolResult result = fixture.tool().execute(
                "{\"symbol_id\":\"java:demo.Service#run()\"}", fixture.context());

        assertTrue(result.isSuccess(), result.getError());
        assertTrue(result.getResult().contains("@Deprecated"), result.getResult());
        assertTrue(result.getResult().contains("void run()"), result.getResult());
        assertTrue(result.getResult().contains("helper();"), result.getResult());
        assertTrue(result.getResult().contains("kind: METHOD"), result.getResult());
    }

    @Test
    void sourceReaderRejectsBareIdsAndLegacySubjectAlias(@TempDir Path repo) throws Exception {
        Fixture fixture = fixture(repo, "strict-input");
        assertFalse(fixture.tool().execute("java:demo.Service#run()", fixture.context()).isSuccess());
        assertFalse(fixture.tool().execute(
                "{\"subject\":\"java:demo.Service#run()\"}", fixture.context()).isSuccess());
    }

    @Test
    void overloadsOnOneLineUseTheRequestedSymbol(@TempDir Path repo) throws Exception {
        Path file = repo.resolve("src/main/java/demo/Overloads.java");
        Files.createDirectories(file.getParent());
        Files.writeString(file,
                "package demo; class Overloads { void run() { zero(); } "
                        + "void run(int value) { one(); } void zero() {} void one() {} }\n");

        Fixture fixture = fixture(repo, "overload-source");
        ToolResult result = fixture.tool().execute(
                query("java:demo.Overloads#run(int)"), fixture.context());

        assertTrue(result.isSuccess(), result.getError());
        assertTrue(result.getResult().contains("void run(int value)"), result.getResult());
        assertTrue(result.getResult().contains("one();"), result.getResult());
        assertFalse(result.getResult().contains("zero();"), result.getResult());
    }

    @Test
    void fieldReturnsWholeDeclarationIncludingAnnotation(@TempDir Path repo) throws Exception {
        Path file = repo.resolve("src/main/java/demo/State.java");
        Files.createDirectories(file.getParent());
        Files.writeString(file, """
                package demo;
                class State {
                    @Deprecated
                    private int counter = 1;
                }
                """);

        Fixture fixture = fixture(repo, "field-source");
        ToolResult result = fixture.tool().execute(
                query("java:demo.State#counter"), fixture.context());

        assertTrue(result.isSuccess(), result.getError());
        assertTrue(result.getResult().contains("@Deprecated"), result.getResult());
        assertTrue(result.getResult().contains("private int counter = 1;"), result.getResult());
        assertTrue(result.getResult().contains("kind: FIELD"), result.getResult());
    }

    @Test
    void typeReturnsClassDefinitionAndAnnotation(@TempDir Path repo) throws Exception {
        Path file = repo.resolve("src/main/java/demo/State.java");
        Files.createDirectories(file.getParent());
        Files.writeString(file, """
                package demo;
                @Deprecated
                class State {
                    private int counter;
                }
                """);

        Fixture fixture = fixture(repo, "type-source");
        ToolResult result = fixture.tool().execute(query("java:demo.State"), fixture.context());

        assertTrue(result.isSuccess(), result.getError());
        assertTrue(result.getResult().contains("@Deprecated"), result.getResult());
        assertTrue(result.getResult().contains("class State"), result.getResult());
        assertTrue(result.getResult().contains("private int counter;"), result.getResult());
        assertTrue(result.getResult().contains("kind: TYPE"), result.getResult());
    }

    @Test
    void frameworkEntrypointReturnsAnnotationExpression(@TempDir Path repo) throws Exception {
        Path file = repo.resolve("src/main/java/demo/Controller.java");
        Files.createDirectories(file.getParent());
        Files.writeString(file, """
                package demo;
                class Controller {
                    @RequestMapping(\"/orders\")
                    void list() {}
                }
                """);

        Fixture fixture = fixture(repo, "framework-source");
        GraphNode entrypoint = fixture.snapshot().graph().nodes().stream()
                .filter(node -> node.kind() == GraphNodeKind.FRAMEWORK_ENTRYPOINT)
                .findFirst()
                .orElseThrow();
        ToolResult result = fixture.tool().execute(query(entrypoint.id()), fixture.context());

        assertTrue(result.isSuccess(), result.getError());
        assertTrue(result.getResult().contains("@RequestMapping(\"/orders\")"), result.getResult());
        assertTrue(result.getResult().contains("owner_id: " + entrypoint.ownerId()), result.getResult());
        assertTrue(result.getResult().contains("kind: FRAMEWORK_ENTRYPOINT"), result.getResult());
    }

    @Test
    void annotationDeclarationIsReadAsType(@TempDir Path repo) throws Exception {
        Path file = repo.resolve("src/main/java/demo/Marker.java");
        Files.createDirectories(file.getParent());
        Files.writeString(file, """
                package demo;
                public @interface Marker { String value(); }
                """);

        Fixture fixture = fixture(repo, "annotation-declaration");
        ToolResult result = fixture.tool().execute(query("java:demo.Marker"), fixture.context());

        assertTrue(result.isSuccess(), result.getError());
        assertTrue(result.getResult().contains("@interface Marker"), result.getResult());
        assertTrue(result.getResult().contains("kind: TYPE"), result.getResult());
    }

    @Test
    void rejectsOversizedSymbolInsteadOfReturningPartialSource(@TempDir Path repo)
            throws Exception {
        Path file = repo.resolve("src/main/java/demo/Huge.java");
        Files.createDirectories(file.getParent());
        Files.writeString(file,
                "package demo; class Huge { void run() { /* "
                        + "x".repeat(17_000) + " */ } }\n");

        Fixture fixture = fixture(repo, "oversized-symbol");
        ToolResult result = fixture.tool().execute(
                query("java:demo.Huge#run()"), fixture.context());

        assertFalse(result.isSuccess());
        assertTrue(result.getError().startsWith("symbol_too_large:"), result.getError());
    }

    @Test
    void rejectsFilePathAndUnknownSymbol(@TempDir Path repo) throws Exception {
        Files.createDirectories(repo.resolve("src/main/java/demo"));
        Files.writeString(repo.resolve("src/main/java/demo/Service.java"),
                "package demo; class Service {}\n");
        Fixture fixture = fixture(repo, "invalid-source-query");

        ToolResult path = fixture.tool().execute("src/main/java/demo/Service.java", fixture.context());
        ToolResult unknown = fixture.tool().execute(
                "{\"symbol_id\":\"java:demo.Missing\"}", fixture.context());

        assertFalse(path.isSuccess());
        assertEquals("缺少 symbol_id", path.getError());
        assertFalse(unknown.isSuccess());
        assertEquals("symbol_not_found: java:demo.Missing", unknown.getError());
    }

    @Test
    void lazyProductionProviderUsesSourceOnlyPath(@TempDir Path repo) throws Exception {
        Path file = repo.resolve("src/main/java/demo/Service.java");
        Files.createDirectories(file.getParent());
        Files.writeString(file, """
                package demo;
                class Service { void run() { helper(); } void helper() {} }
                """);

        ProjectSnapshotProvider provider = new ProjectSnapshotManager()
                .lazyProvider(com.codeguard.agent.graph.ProjectKey.of(repo, "source-fast"));
        SymbolSourceReader tool = new SymbolSourceReader((SourceSnapshotProvider) provider);

        ToolResult result = tool.execute(
                query("java:demo.Service#run()"), new AgentContext(repo));

        assertTrue(result.isSuccess(), result.getError());
        assertTrue(result.getResult().contains("helper();"), result.getResult());

        ToolResult typeResult = tool.execute(
                query("java:demo.Service"), new AgentContext(repo));
        assertTrue(typeResult.isSuccess(), typeResult.getError());
        assertTrue(typeResult.getResult().contains("class Service"), typeResult.getResult());
    }

    @Test
    void lazyProductionProviderPreservesFrameworkEntrypointFallback(@TempDir Path repo)
            throws Exception {
        Path file = repo.resolve("src/main/java/demo/Service.java");
        Files.createDirectories(file.getParent());
        Files.writeString(file, """
                package demo;
                class Service {
                    @RequestMapping("/run")
                    void run() {}
                }
                """);

        ProjectSnapshotProvider provider = new ProjectSnapshotManager()
                .lazyProvider(ProjectKey.of(repo, "framework-fallback"));
        ProjectSnapshot index = provider.load(
                "read_symbol", query("framework:placeholder"));
        GraphNode entrypoint = index.graph().nodes().stream()
                .filter(node -> node.kind() == GraphNodeKind.FRAMEWORK_ENTRYPOINT)
                .findFirst()
                .orElseThrow();
        SymbolSourceReader tool = new SymbolSourceReader((SourceSnapshotProvider) provider);

        ToolResult result = tool.execute(query(entrypoint.id()), new AgentContext(repo));

        assertTrue(result.isSuccess(), result.getError());
        assertTrue(result.getResult().contains("@RequestMapping"), result.getResult());
    }
}
