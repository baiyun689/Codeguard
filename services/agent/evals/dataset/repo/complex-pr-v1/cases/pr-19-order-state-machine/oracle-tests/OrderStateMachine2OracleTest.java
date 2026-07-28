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
final class OrderStateMachine2OracleTest {
    @Test
    @DisplayName("触发: 并发修改后提交映射结果；后果: 覆盖另一请求已提交的状态")
    void mapOrderUpdate_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/OrderStateMachineService.java"));
        assertAll(
        () -> assertTrue(source.contains("Order current = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"), "missing seeded evidence: Order current = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"),
        () -> assertTrue(source.contains("Order mapped = new Order(current.id(), current.tenantId(), current.total(), request.get(\"status\"));"), "missing seeded evidence: Order mapped = new Order(current.id(), current.tenantId(), current.total(), request.get(\"status\"));"),
        () -> assertTrue(source.contains("orders.save(mapped);"), "missing seeded evidence: orders.save(mapped);")

        );
    }
}
