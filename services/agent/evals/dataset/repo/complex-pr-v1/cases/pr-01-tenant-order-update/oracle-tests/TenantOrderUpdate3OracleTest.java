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
final class TenantOrderUpdate3OracleTest {
    @Test
    @DisplayName("触发: 保存订单失败或事务随后回滚；后果: 审计系统记录不存在的成功操作")
    void prematureAudit_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/TenantOrderUpdateService.java"));
        assertAll(
        () -> assertTrue(source.contains("Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"), "missing seeded evidence: Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"),
        () -> assertTrue(source.contains("audit.record(context.tenantId(), \"ORDER_UPDATED\", order.id());"), "missing seeded evidence: audit.record(context.tenantId(), \"ORDER_UPDATED\", order.id());"),
        () -> assertTrue(source.contains("order.status(request.get(\"status\"));"), "missing seeded evidence: order.status(request.get(\"status\"));")
        );
    }
}
