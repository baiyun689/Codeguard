package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.ToolResult;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.CompletableFuture;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * get_file_content 工具 + 文件访问护栏的工程正确性测试。
 * 重点覆盖真实路径护栏：链接可留在仓库内，但绝不能逃出仓库。
 */
class GetFileContentToolTest {

    private GetFileContentTool toolFor(Path repoRoot, Set<String> allowed) {
        return new GetFileContentTool(new FileAccessSandbox(repoRoot));
    }

    private AgentContext ctx(Path repoRoot, Set<String> allowed) {
        return new AgentContext(repoRoot, allowed);
    }

    @Test
    void readsFileWithinScope(@TempDir Path repo) throws IOException {
        Path f = repo.resolve("src/App.java");
        Files.createDirectories(f.getParent());
        Files.writeString(f, "class App {}");

        Set<String> allowed = Set.of("src/App.java");
        ToolResult r = toolFor(repo, allowed).execute("src/App.java", ctx(repo, allowed));

        assertTrue(r.isSuccess());
        assertTrue(r.getResult().contains("class App {}"));
    }

    @Test
    void rejectsPathTraversal(@TempDir Path repo) {
        Set<String> allowed = Set.of("src/App.java");
        ToolResult r = toolFor(repo, allowed).execute("../secret.txt", ctx(repo, allowed));

        assertFalse(r.isSuccess());
        assertEquals("rejected_path_outside_repository", r.getError());
    }

    @Test
    void readsDiffExternalSourceFile(@TempDir Path repo) throws IOException {
        // 护栏放宽后(design.md D5):repo 内的源码文件即便不在 diff 改动集合里,也应可读
        // —— 这正是 get_repo_map 指向的、diff 之外定义文件的读取路径。
        Path f = repo.resolve("src/Other.java");
        Files.createDirectories(f.getParent());
        Files.writeString(f, "class Other {}");

        Set<String> allowed = Set.of("src/App.java"); // Other.java 不在内
        ToolResult r = toolFor(repo, allowed).execute("src/Other.java", ctx(repo, allowed));

        assertTrue(r.isSuccess());
        assertTrue(r.getResult().contains("class Other {}"));
    }

    @Test
    void readsTestSourceWithoutTreatingItAsProductionEvidence(
            @TempDir Path repo
    ) throws IOException {
        Path file = repo.resolve("src/test/java/demo/ServiceTest.java");
        Files.createDirectories(file.getParent());
        Files.writeString(file, "class ServiceTest {}");

        ToolResult result = toolFor(repo, Set.of()).execute(
                "src/test/java/demo/ServiceTest.java",
                ctx(repo, Set.of()));

        assertTrue(result.isSuccess());
        assertTrue(result.getResult().contains("class ServiceTest {}"));
    }

    @Test
    void rejectsNonSourceFile(@TempDir Path repo) throws IOException {
        // 非源码类型(.env / .conf 等配置/密钥文件)即便在仓库内也拒读,守住放宽后的边界。
        // 注意:.properties 已在 b589f95 被加入源码白名单,改用 .env 验证。
        Path f = repo.resolve(".env");
        Files.writeString(f, "DB_PASSWORD=secret");

        Set<String> allowed = Set.of(".env");
        ToolResult r = toolFor(repo, allowed).execute(".env", ctx(repo, allowed));

        assertFalse(r.isSuccess());
        assertEquals("rejected_file_type", r.getError());
    }

    @Test
    void rejectsOversizeFile(@TempDir Path repo) throws IOException {
        Path f = repo.resolve("Big.java");
        Files.writeString(f, "x".repeat(100_001));

        Set<String> allowed = Set.of("Big.java");
        ToolResult r = toolFor(repo, allowed).execute("Big.java", ctx(repo, allowed));

        assertFalse(r.isSuccess());
        assertTrue(r.getError().contains("过大"));
    }

    @Test
    void rejectsNonexistentFile(@TempDir Path repo) {
        // 在允许集合内,但磁盘上不存在。
        Set<String> allowed = Set.of("src/Ghost.java");
        ToolResult r = toolFor(repo, allowed).execute("src/Ghost.java", ctx(repo, allowed));

        assertFalse(r.isSuccess());
        assertEquals("missing_file", r.getError());
    }

    @Test
    void rejectsEmptyPath(@TempDir Path repo) {
        Set<String> allowed = Set.of("src/App.java");
        ToolResult r = toolFor(repo, allowed).execute("  ", ctx(repo, allowed));

        assertFalse(r.isSuccess());
    }

    @Test
    void followsSymlinkThatStillResolvesInsideRepository(@TempDir Path repo) throws IOException {
        Path target = repo.resolve("src/Real.java");
        Files.createDirectories(target.getParent());
        Files.writeString(target, "class Real {}");
        Path link = repo.resolve("src/Linked.java");
        createSymbolicLink(link, Path.of("Real.java"));

        ToolResult result = toolFor(repo, Set.of()).execute("src/Linked.java", ctx(repo, Set.of()));

        assertTrue(result.isSuccess());
        assertTrue(result.getResult().contains("class Real {}"));
    }

    @Test
    void readsRepositoryInternalSymlinkThroughSnapshot(@TempDir Path repo) throws Exception {
        Path target = repo.resolve("src/Real.java");
        Files.createDirectories(target.getParent());
        Files.writeString(target, "class Real {}");
        Path link = repo.resolve("src/Linked.java");
        createSymbolicLink(link, Path.of("Real.java"));
        CompletableFuture<com.codeguard.agent.graph.ProjectSnapshot> snapshot =
                new com.codeguard.agent.graph.ProjectSnapshotManager()
                        .getOrBuild(com.codeguard.agent.graph.ProjectKey.of(repo, "test"));
        GetFileContentTool tool = new GetFileContentTool(new FileAccessSandbox(repo), snapshot);

        ToolResult result = tool.execute("src/Linked.java", ctx(repo, Set.of()));

        assertTrue(result.isSuccess(), result.getError());
        assertTrue(result.getResult().contains("class Real {}"));
    }

    @Test
    void rejectsSymlinkThatResolvesOutsideRepository(@TempDir Path repo, @TempDir Path outside)
            throws IOException {
        Path target = outside.resolve("Secret.java");
        Files.writeString(target, "class Secret {}");
        Path link = repo.resolve("src/Linked.java");
        Files.createDirectories(link.getParent());
        createSymbolicLink(link, target);

        ToolResult result = toolFor(repo, Set.of()).execute("src/Linked.java", ctx(repo, Set.of()));

        assertFalse(result.isSuccess());
        assertEquals("rejected_path_outside_repository", result.getError());
        assertFalse(result.getError().contains(outside.toString()));
    }

    @Test
    void rejectsFileUnderParentDirectorySymlinkOutsideRepository(
            @TempDir Path repo,
            @TempDir Path outside
    ) throws IOException {
        Path target = outside.resolve("External.java");
        Files.writeString(target, "class External {}");
        Path linkDirectory = repo.resolve("src");
        createSymbolicLink(linkDirectory, outside);

        ToolResult result = toolFor(repo, Set.of()).execute("src/External.java", ctx(repo, Set.of()));

        assertFalse(result.isSuccess());
        assertEquals("rejected_path_outside_repository", result.getError());
    }

    private static void createSymbolicLink(Path link, Path target) throws IOException {
        try {
            Files.createSymbolicLink(link, target);
        } catch (java.nio.file.FileSystemException | UnsupportedOperationException exception) {
            Assumptions.assumeTrue(false, "当前系统不允许测试创建符号链接");
        }
    }

}
