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

class GetDiffASTToolTest {

    @Test
    void outputsAstForJavaFiles(@TempDir Path repo) throws Exception {
        Path f = repo.resolve("Service.java");
        String code = """
            public class Service {
                public void run(String input) {
                    if (input != null) {
                        helper.process(input);
                    }
                }
            }
            """;
        Files.writeString(f, code, StandardCharsets.UTF_8);

        FileAccessSandbox sandbox = new FileAccessSandbox(repo, Set.of("Service.java"));
        AgentContext ctx = new AgentContext(repo, Set.of("Service.java"));
        String diffText = "diff --git a/Service.java b/Service.java\n"
                + "--- a/Service.java\n"
                + "+++ b/Service.java\n"
                + "@@ -2,0 +3,3 @@\n"
                + "+    if (input != null) {\n"
                + "+        helper.process(input);\n"
                + "+    }\n";
        ToolResult r = new GetDiffASTTool(sandbox).execute(diffText, ctx);
        assertTrue(r.isSuccess());
        String output = r.getResult();
        assertTrue(output.contains("AST for: Service.java"));
        assertTrue(output.contains("class: Service"));
        assertTrue(output.contains("run"));
    }

    @Test
    void skipsNonJavaFiles(@TempDir Path repo) throws Exception {
        Path f = repo.resolve("config.xml");
        Files.writeString(f, "<config/>", StandardCharsets.UTF_8);

        FileAccessSandbox sandbox = new FileAccessSandbox(repo, Set.of("config.xml"));
        AgentContext ctx = new AgentContext(repo, Set.of("config.xml"));
        ToolResult r = new GetDiffASTTool(sandbox).execute("diff text", ctx);
        assertTrue(r.isSuccess());
        assertTrue(r.getResult().contains("无可解析的 Java AST 上下文"));
    }

    @Test
    void handlesParseFailureGracefully(@TempDir Path repo) throws Exception {
        Path good = repo.resolve("Good.java");
        Files.writeString(good, "class Good {}", StandardCharsets.UTF_8);
        Path bad = repo.resolve("Bad.java");
        Files.writeString(bad, "not java {{{", StandardCharsets.UTF_8);

        FileAccessSandbox sandbox = new FileAccessSandbox(repo, Set.of("Good.java", "Bad.java"));
        AgentContext ctx = new AgentContext(repo, Set.of("Good.java", "Bad.java"));
        ToolResult r = new GetDiffASTTool(sandbox).execute("diff --git a/Good.java b/Good.java\n--- a/Good.java\n+++ b/Good.java\n@@ -1,0 +1,1 @@\n+class Good {}\n", ctx);
        assertTrue(r.isSuccess());
        String output = r.getResult();
        assertTrue(output.contains("AST for: Good.java"));
        assertFalse(output.contains("AST for: Bad.java"));
    }

    @Test
    void emptyAllowedFiles(@TempDir Path repo) {
        FileAccessSandbox sandbox = new FileAccessSandbox(repo, Set.of());
        AgentContext ctx = new AgentContext(repo, Set.of());
        ToolResult r = new GetDiffASTTool(sandbox).execute("", ctx);
        assertTrue(r.isSuccess());
        assertTrue(r.getResult().contains("无可解析的 Java AST 上下文"));
    }
}
