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

class FindCallersToolTest {

    private static ToolResult execute(Path repo, Set<String> allowedFiles, String input) {
        FileAccessSandbox sandbox = new FileAccessSandbox(repo, allowedFiles);
        AgentContext ctx = new AgentContext(repo, allowedFiles);
        return new FindCallersTool(sandbox).execute(input, ctx);
    }

    @Test
    void missingHashSeparator(@TempDir Path repo) {
        ToolResult r = execute(repo, Set.of("src/Foo.java"), "no_separator_here");
        assertFalse(r.isSuccess());
        assertTrue(r.getError().contains("#"));
    }

    @Test
    void emptyInputReturnsError(@TempDir Path repo) {
        ToolResult r = execute(repo, Set.of(), "");
        assertFalse(r.isSuccess());
        assertTrue(r.getError().contains("缺少参数"));
    }

    @Test
    void targetFileNotFound(@TempDir Path repo) {
        ToolResult r = execute(repo, Set.of(), "src/Nonexistent.java#foo");
        assertFalse(r.isSuccess());
        assertTrue(r.getError().contains("不存在"));
    }

    @Test
    void findsCallersInRepository(@TempDir Path repo) throws Exception {
        // 目标文件:定义 calculatePrice
        Path target = repo.resolve("src/main/java/OrderService.java");
        Files.createDirectories(target.getParent());
        Files.writeString(target,
                "package com.example;\n"
                + "public class OrderService {\n"
                + "  public int calculatePrice(int qty) { return qty * 10; }\n"
                + "}\n",
                StandardCharsets.UTF_8);

        // 调用方文件
        Path caller = repo.resolve("src/main/java/PaymentGateway.java");
        Files.writeString(caller,
                "package com.example;\n"
                + "public class PaymentGateway {\n"
                + "  public void process(OrderService svc) {\n"
                + "    int price = svc.calculatePrice(5);\n"
                + "  }\n"
                + "}\n",
                StandardCharsets.UTF_8);

        ToolResult r = execute(repo, Set.of(),
                "src/main/java/OrderService.java#calculatePrice");
        assertTrue(r.isSuccess());
        String out = r.getResult();
        assertTrue(out.contains("PaymentGateway"), out);
        assertTrue(out.contains("calculatePrice"), out);
    }

    @Test
    void noCallersFoundReturnsEmpty(@TempDir Path repo) throws Exception {
        Path target = repo.resolve("OnlyHere.java");
        Files.writeString(target,
                "class OnlyHere {\n"
                + "  void lonely() {}\n"
                + "}\n",
                StandardCharsets.UTF_8);
        ToolResult r = execute(repo, Set.of(), "OnlyHere.java#lonely");
        assertTrue(r.isSuccess());
        assertTrue(r.getResult().contains("未找到"));
    }
}
