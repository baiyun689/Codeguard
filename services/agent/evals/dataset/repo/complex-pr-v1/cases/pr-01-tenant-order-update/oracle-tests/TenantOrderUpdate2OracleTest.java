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
final class TenantOrderUpdate2OracleTest {
    @Test
    @DisplayName("触发: 调用方提交自行选择的 total 或内部状态；后果: 订单金额或状态机被绕过")
    void mutableProjection_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/TenantOrderUpdateService.java"));
        assertAll(
        () -> assertTrue(source.contains("Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"), "missing seeded evidence: Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"),
        () -> assertTrue(source.contains("order.total(new BigDecimal(request.get(\"total\")));"), "missing seeded evidence: order.total(new BigDecimal(request.get(\"total\")));"),
        () -> assertTrue(source.contains("order.status(request.get(\"status\"));"), "missing seeded evidence: order.status(request.get(\"status\"));")

        );
    }
}
