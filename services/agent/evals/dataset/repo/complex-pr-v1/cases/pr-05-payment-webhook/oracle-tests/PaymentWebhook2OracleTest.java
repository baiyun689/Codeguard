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
final class PaymentWebhook2OracleTest {
    @Test
    @DisplayName("触发: 攻击者重放曾经合法的支付事件；后果: 订单状态或退款副作用重复执行")
    void acceptWebhookEvent_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/PaymentWebhookService.java"));
        assertAll(
        () -> assertTrue(source.contains("String eventId = request.get(\"eventId\");"), "missing seeded evidence: String eventId = request.get(\"eventId\");"),
        () -> assertTrue(source.contains("if (cache.get(\"webhook:\" + eventId).isPresent()) return \"duplicate\";"), "missing seeded evidence: if (cache.get(\"webhook:\" + eventId).isPresent()) return \"duplicate\";"),
        () -> assertTrue(source.contains("cache.put(\"webhook:\" + eventId, \"processed\", Duration.ofDays(30));"), "missing seeded evidence: cache.put(\"webhook:\" + eventId, \"processed\", Duration.ofDays(30));")
        );
    }
}
