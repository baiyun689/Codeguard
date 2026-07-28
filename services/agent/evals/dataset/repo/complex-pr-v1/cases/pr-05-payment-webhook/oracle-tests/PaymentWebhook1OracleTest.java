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
final class PaymentWebhook1OracleTest {
    @Test
    @DisplayName("触发: JSON 空白、字段顺序或数字格式被解析器规范化；后果: 合法回调被拒绝或错误实现下可绕过认证")
    void verifyWebhookSignature_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/PaymentWebhookService.java"));
        assertAll(
        () -> assertTrue(source.contains(".map(entry -> entry.getKey() + \"=\" + entry.getValue()).collect(Collectors.joining(\"&\"));"), "missing seeded evidence: .map(entry -> entry.getKey() + \"=\" + entry.getValue()).collect(Collectors.joining(\"&\"));"),
        () -> assertTrue(source.contains("request.get(\"signature\").getBytes(StandardCharsets.UTF_8));"), "missing seeded evidence: request.get(\"signature\").getBytes(StandardCharsets.UTF_8));")
        );
    }
}
