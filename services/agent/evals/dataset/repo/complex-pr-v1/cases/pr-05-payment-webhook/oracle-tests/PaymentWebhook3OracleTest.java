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
final class PaymentWebhook3OracleTest {
    @Test
    @DisplayName("触发: 订单保存失败；后果: 重试被完成标记挡住，支付状态永久丢失")
    void applyPaymentEvent_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/PaymentWebhookService.java"));
        assertAll(
        () -> assertTrue(source.contains("cache.put(\"webhook:\" + request.get(\"eventId\"), \"processed\", Duration.ofDays(30));"), "missing seeded evidence: cache.put(\"webhook:\" + request.get(\"eventId\"), \"processed\", Duration.ofDays(30));"),
        () -> assertTrue(source.contains("Order order = orders.findByTenantAndId(request.get(\"tenantId\"), request.get(\"orderId\")).orElseThrow();"), "missing seeded evidence: Order order = orders.findByTenantAndId(request.get(\"tenantId\"), request.get(\"orderId\")).orElseThrow();"),
        () -> assertTrue(source.contains("orders.save(order);"), "missing seeded evidence: orders.save(order);")
        );
    }
}
