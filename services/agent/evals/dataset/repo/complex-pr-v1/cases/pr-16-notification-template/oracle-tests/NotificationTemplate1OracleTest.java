package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertAll;
import static org.junit.jupiter.api.Assertions.assertTrue;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * Evaluator-only static contract oracle. It is excluded from the
 * reviewed project snapshot.
 */
final class NotificationTemplate1OracleTest {
    @Test
    @DisplayName("触发: 模板包含可执行表达式；后果: 读取服务数据或执行危险方法")
    void renderNotification_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/NotificationTemplateService.java"));
        assertAll(
        () -> assertTrue(source.contains("return parser.parseExpression(request.get(\"template\")).getValue(request);"), "missing seeded evidence: return parser.parseExpression(request.get(\"template\")).getValue(request);")
        );
    }
}
