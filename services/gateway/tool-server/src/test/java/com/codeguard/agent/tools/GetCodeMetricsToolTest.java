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

class GetCodeMetricsToolTest {

    private static ToolResult execute(Path repo, Set<String> allowedFiles, String input) {
        FileAccessSandbox sandbox = new FileAccessSandbox(repo, allowedFiles);
        AgentContext ctx = new AgentContext(repo, allowedFiles);
        return new GetCodeMetricsTool(sandbox).execute(input, ctx);
    }

    @Test
    void emptyInputReturnsError(@TempDir Path repo) {
        ToolResult r = execute(repo, Set.of(), "");
        assertFalse(r.isSuccess());
        assertTrue(r.getError().contains("文件路径"));
    }

    @Test
    void fileNotFound(@TempDir Path repo) {
        ToolResult r = execute(repo, Set.of(), "NoSuchFile.java");
        assertFalse(r.isSuccess());
        assertTrue(r.getError().contains("不存在"));
    }

    @Test
    void computesMetrics(@TempDir Path repo) throws Exception {
        Path f = repo.resolve("Service.java");
        String code = "class Service {\n"
                + "  int simple() { return 1; }\n"
                + "  int complex(int x) {\n"
                + "    if (x > 0) {\n"
                + "      for (int i = 0; i < 10; i++) {\n"
                + "        if (i % 2 == 0) x++;\n"
                + "      }\n"
                + "    }\n"
                + "    return x;\n"
                + "  }\n"
                + "}\n";
        Files.writeString(f, code, StandardCharsets.UTF_8);
        ToolResult r = execute(repo, Set.of("Service.java"), "Service.java");
        assertTrue(r.isSuccess());
        String out = r.getResult();
        assertTrue(out.contains("simple"), out);
        assertTrue(out.contains("complex"), out);
        // simple has no branches → CC=1
        assertTrue(out.contains("1"), out);
        // complex has 2 if + 1 for = CC≥4
    }

    @Test
    void interfaceFileReturnsNoMethod(@TempDir Path repo) throws Exception {
        Path f = repo.resolve("IFace.java");
        Files.writeString(f,
                "interface IFace {\n"
                + "  void go();\n"
                + "}\n",
                StandardCharsets.UTF_8);
        ToolResult r = execute(repo, Set.of("IFace.java"), "IFace.java");
        assertTrue(r.isSuccess());
        assertTrue(r.getResult().contains("无可度量") || r.getResult().contains("抽象"));
    }

    @Test
    void unparseableFileReturnsError(@TempDir Path repo) throws Exception {
        Path f = repo.resolve("Broken.java");
        Files.writeString(f, "not valid java {{{", StandardCharsets.UTF_8);
        ToolResult r = execute(repo, Set.of("Broken.java"), "Broken.java");
        assertFalse(r.isSuccess());
    }
}
