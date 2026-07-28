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
final class TenantOrderUpdate1OracleTest {
    @Test
    @DisplayName("触发: 另一租户用户提交可猜测的订单 ID；后果: 跨租户订单状态被修改")
    void tenantLookup_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/TenantOrderUpdateService.java"));
        assertAll(
        () -> assertTrue(source.contains("Order order = orders.findById(request.get(\"orderId\")).orElseThrow();"), "missing seeded evidence: Order order = orders.findById(request.get(\"orderId\")).orElseThrow();"),
        () -> assertTrue(source.contains("order.status(request.get(\"status\"));"), "missing seeded evidence: order.status(request.get(\"status\"));"),
        () -> assertTrue(source.contains("orders.save(order);"), "missing seeded evidence: orders.save(order);")

        );
    }
}
