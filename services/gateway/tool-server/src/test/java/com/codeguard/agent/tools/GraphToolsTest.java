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
                new FileAccessSandbox(repo, Set.of()), snapshot);
        AgentContext context = new AgentContext(repo, Set.of());

        ToolResult missing = tool.execute("GuessedController.java", context);

        assertFalse(missing.isSuccess());
        assertTrue(missing.getError().contains("unconfirmed_path"), missing.getError());
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

        assertTrue(impact.isSuccess(), impact.getError());
        assertTrue(impact.getResult().contains("\"status\":\"unknown\""), impact.getResult());
        assertTrue(impact.getResult().contains("\"coverage\":\"partial\""), impact.getResult());
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
