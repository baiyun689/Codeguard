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
final class OrderEvents2OracleTest {
    @Test
    @DisplayName("触发: 任意下游暂时失败；后果: 消息 offset 前进且事件永久丢失")
    void consumeOrderEvent_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/OrderEventsService.java"));
        assertAll(
        () -> assertTrue(source.contains("Order order = orders.findByTenantAndId(request.get(\"tenantId\"), request.get(\"orderId\")).orElseThrow();"), "missing seeded evidence: Order order = orders.findByTenantAndId(request.get(\"tenantId\"), request.get(\"orderId\")).orElseThrow();"),
        () -> assertTrue(source.contains("order.status(request.get(\"status\"));"), "missing seeded evidence: order.status(request.get(\"status\"));"),
        () -> assertTrue(source.contains("orders.save(order);"), "missing seeded evidence: orders.save(order);")
        );
    }
}
