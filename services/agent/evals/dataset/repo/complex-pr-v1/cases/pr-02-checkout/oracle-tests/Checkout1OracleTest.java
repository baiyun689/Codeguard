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
final class Checkout1OracleTest {
    @Test
    @DisplayName("触发: 支付成功后库存版本冲突或库存不足；后果: 用户被扣款但订单无法履约")
    void uncompensatedCharge_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/CheckoutService.java"));
        assertAll(
        () -> assertTrue(source.contains("Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"), "missing seeded evidence: Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"),
        () -> assertTrue(source.contains("String paymentId = payments.charge(context.tenantId(), order.id(), order.total(), request.get(\"requestId\"));"), "missing seeded evidence: String paymentId = payments.charge(context.tenantId(), order.id(), order.total(), request.get(\"requestId\"));"),
        () -> assertTrue(source.contains("InventoryItem item = inventory.findBySku(request.get(\"sku\")).orElseThrow();"), "missing seeded evidence: InventoryItem item = inventory.findBySku(request.get(\"sku\")).orElseThrow();")
        );
    }
}
