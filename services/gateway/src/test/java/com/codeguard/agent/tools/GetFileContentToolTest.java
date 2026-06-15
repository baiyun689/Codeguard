package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.ToolResult;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * get_file_content 工具 + 文件访问护栏的工程正确性测试。
 * 重点覆盖 spec 的护栏场景:正常读取 + 四类拒绝(穿越 / 范围外 / 超大 / 不存在)。
 */
class GetFileContentToolTest {

    private GetFileContentTool toolFor(Path repoRoot, Set<String> allowed) {
        return new GetFileContentTool(new FileAccessSandbox(repoRoot, allowed));
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
        assertTrue(r.getError().contains(".."));
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
    void rejectsNonSourceFile(@TempDir Path repo) throws IOException {
        // 非源码类型(配置/密钥/文本)即便在仓库内也拒读,守住放宽后的边界。
        Path f = repo.resolve("application.properties");
        Files.writeString(f, "db.password=secret");

        Set<String> allowed = Set.of("application.properties");
        ToolResult r = toolFor(repo, allowed).execute("application.properties", ctx(repo, allowed));

        assertFalse(r.isSuccess());
        assertTrue(r.getError().contains("源码"));
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
        assertTrue(r.getError().contains("不存在"));
    }

    @Test
    void rejectsEmptyPath(@TempDir Path repo) {
        Set<String> allowed = Set.of("src/App.java");
        ToolResult r = toolFor(repo, allowed).execute("  ", ctx(repo, allowed));

        assertFalse(r.isSuccess());
    }

    @Test
    void sandboxScopeCheckNormalizesSeparators(@TempDir Path repo) throws IOException {
        Path f = repo.resolve("src/App.java");
        Files.createDirectories(f.getParent());
        Files.writeString(f, "ok");

        FileAccessSandbox sandbox = new FileAccessSandbox(repo, Set.of("src/App.java"));
        // 反斜杠输入也应被规范化后命中白名单(跨平台)。
        assertTrue(sandbox.isFileInScope("src\\App.java"));
        assertFalse(sandbox.isFileInScope("src/Missing.java"));
    }
}
