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
final class RefundFlow2OracleTest {
    @Test
    @DisplayName("触发: 两个退款请求同时通过余额检查；后果: 重复退款造成资金损失")
    void refundRemainingAmount_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/RefundFlowService.java"));
        assertAll(
        () -> assertTrue(source.contains("Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"), "missing seeded evidence: Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"),
        () -> assertTrue(source.contains("BigDecimal amount = new BigDecimal(request.get(\"amount\"));"), "missing seeded evidence: BigDecimal amount = new BigDecimal(request.get(\"amount\"));"),
        () -> assertTrue(source.contains("if (amount.compareTo(order.refundable()) > 0) throw new IllegalArgumentException(\"too large\");"), "missing seeded evidence: if (amount.compareTo(order.refundable()) > 0) throw new IllegalArgumentException(\"too large\");")
        );
    }
}
