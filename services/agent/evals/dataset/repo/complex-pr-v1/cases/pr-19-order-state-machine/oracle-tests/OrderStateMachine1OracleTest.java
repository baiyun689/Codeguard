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
final class OrderStateMachine1OracleTest {
    @Test
    @DisplayName("触发: 从取消状态请求数值更大的状态；后果: 已终止订单重新进入履约")
    void transitionOrder_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/OrderStateMachineService.java"));
        assertAll(
        () -> assertTrue(source.contains("Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"), "missing seeded evidence: Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"),
        () -> assertTrue(source.contains("if (OrderStatus.valueOf(request.get(\"status\")).ordinal() > OrderStatus.valueOf(order.status()).ordinal()) {"), "missing seeded evidence: if (OrderStatus.valueOf(request.get(\"status\")).ordinal() > OrderStatus.valueOf(order.status()).ordinal()) {"),
        () -> assertTrue(source.contains("order.status(request.get(\"status\"));"), "missing seeded evidence: order.status(request.get(\"status\"));")
        );
    }
}
