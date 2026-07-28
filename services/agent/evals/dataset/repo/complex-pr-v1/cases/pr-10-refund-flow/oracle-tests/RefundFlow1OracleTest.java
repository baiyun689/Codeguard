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
final class RefundFlow1OracleTest {
    @Test
    @DisplayName("触发: 一笔订单执行多次部分退款；后果: 累计退款超过实际支付金额")
    void refundAgainstOrder_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/RefundFlowService.java"));
        assertAll(
        () -> assertTrue(source.contains("Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"), "missing seeded evidence: Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"),
        () -> assertTrue(source.contains("BigDecimal amount = new BigDecimal(request.get(\"amount\"));"), "missing seeded evidence: BigDecimal amount = new BigDecimal(request.get(\"amount\"));"),
        () -> assertTrue(source.contains("if (amount.compareTo(order.total()) > 0) throw new IllegalArgumentException(\"too large\");"), "missing seeded evidence: if (amount.compareTo(order.total()) > 0) throw new IllegalArgumentException(\"too large\");")

        );
    }
}
