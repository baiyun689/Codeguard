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
final class Checkout3OracleTest {
    @Test
    @DisplayName("触发: 两个租户使用相同 requestId；后果: 一个租户收到另一租户的支付结果或请求被错误抑制")
    void sharedIdempotency_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/CheckoutService.java"));
        assertAll(
        () -> assertTrue(source.contains("String key = \"checkout:\" + request.get(\"requestId\");"), "missing seeded evidence: String key = \"checkout:\" + request.get(\"requestId\");"),
        () -> assertTrue(source.contains("return cache.get(key).orElseGet(() -> {"), "missing seeded evidence: return cache.get(key).orElseGet(() -> {"),
        () -> assertTrue(source.contains("String result = payments.charge(context.tenantId(), request.get(\"orderId\"),"), "missing seeded evidence: String result = payments.charge(context.tenantId(), request.get(\"orderId\"),")
        );
    }
}
