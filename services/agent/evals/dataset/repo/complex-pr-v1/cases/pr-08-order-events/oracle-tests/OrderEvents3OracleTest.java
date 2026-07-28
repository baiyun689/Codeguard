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
final class OrderEvents3OracleTest {
    @Test
    @DisplayName("触发: 网络重试导致事件乱序；后果: 订单回退到旧状态")
    void applyVersionedEvent_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/OrderEventsService.java"));
        assertAll(
        () -> assertTrue(source.contains("Order order = orders.findByTenantAndId(request.get(\"tenantId\"), request.get(\"orderId\")).orElseThrow();"), "missing seeded evidence: Order order = orders.findByTenantAndId(request.get(\"tenantId\"), request.get(\"orderId\")).orElseThrow();"),
        () -> assertTrue(source.contains("order.status(request.get(\"status\"));"), "missing seeded evidence: order.status(request.get(\"status\"));"),
        () -> assertTrue(source.contains("order.version(Long.parseLong(request.get(\"version\")));"), "missing seeded evidence: order.version(Long.parseLong(request.get(\"version\")));")

        );
    }
}
