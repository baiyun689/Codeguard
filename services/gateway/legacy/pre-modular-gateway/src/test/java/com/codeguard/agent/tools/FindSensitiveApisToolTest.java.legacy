package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.ToolResult;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.*;

class FindSensitiveApisToolTest {

    private static ToolResult execute(Path repo, Set<String> allowedFiles, String input) {
        FileAccessSandbox sandbox = new FileAccessSandbox(repo, allowedFiles);
        AgentContext ctx = new AgentContext(repo, allowedFiles);
        return new FindSensitiveApisTool(sandbox).execute(input, ctx);
    }

    @Test
    void detectsSqlExecution(@TempDir Path repo) throws Exception {
        Path f = repo.resolve("Dao.java");
        String code = "class Dao {\n"
                + "  void q(String sql) {\n"
                + "    java.sql.Statement stmt = null;\n"
                + "    stmt.executeQuery(sql);\n"
                + "  }\n"
                + "}\n";
        Files.writeString(f, code, StandardCharsets.UTF_8);
        ToolResult r = execute(repo, Set.of("Dao.java"), "");
        assertTrue(r.isSuccess());
        assertTrue(r.getResult().contains("executeQuery"));
        assertTrue(r.getResult().contains("HIGH"));
    }

    @Test
    void detectsRuntimeExec(@TempDir Path repo) throws Exception {
        Path f = repo.resolve("Runner.java");
        String code = "class Runner {\n"
                + "  void run(String cmd) throws Exception {\n"
                + "    Runtime.getRuntime().exec(cmd);\n"
                + "  }\n"
                + "}\n";
        Files.writeString(f, code, StandardCharsets.UTF_8);
        ToolResult r = execute(repo, Set.of("Runner.java"), "");
        assertTrue(r.isSuccess());
        assertTrue(r.getResult().contains("exec"));
        assertTrue(r.getResult().contains("HIGH"));
    }

    @Test
    void cleanFileReturnsEmpty(@TempDir Path repo) throws Exception {
        Path f = repo.resolve("Clean.java");
        String code = "class Clean {\n"
                + "  int add(int a, int b) { return a + b; }\n"
                + "}\n";
        Files.writeString(f, code, StandardCharsets.UTF_8);
        ToolResult r = execute(repo, Set.of("Clean.java"), "");
        assertTrue(r.isSuccess());
        assertTrue(r.getResult().contains("未发现危险"));
    }

    @Test
    void emptyAllowedFilesReturnsEmpty(@TempDir Path repo) {
        ToolResult r = execute(repo, Set.of(), "");
        assertTrue(r.isSuccess());
        assertTrue(r.getResult().contains("无可扫描"));
    }

    @Test
    void skipsUnparseableFile(@TempDir Path repo) throws Exception {
        // valid file
        Path good = repo.resolve("Good.java");
        Files.writeString(good, "class Good {}", StandardCharsets.UTF_8);
        // unparseable file
        Path bad = repo.resolve("Bad.java");
        Files.writeString(bad, "not valid java at all {{{", StandardCharsets.UTF_8);
        ToolResult r = execute(repo, Set.of("Good.java", "Bad.java"), "");
        assertTrue(r.isSuccess());
        assertTrue(r.getResult().contains("跳过"));
    }
}
