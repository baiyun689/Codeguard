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
final class OrderEvents1OracleTest {
    @Test
    @DisplayName("触发: 发布成功后数据库提交失败；后果: 消费者观察到不存在的订单状态")
    void changeOrderStatus_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/OrderEventsService.java"));
        assertAll(
        () -> assertTrue(source.contains("Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"), "missing seeded evidence: Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"),
        () -> assertTrue(source.contains("events.publish(\"order.status\", order.id(), request.get(\"status\"));"), "missing seeded evidence: events.publish(\"order.status\", order.id(), request.get(\"status\"));"),
        () -> assertTrue(source.contains("order.status(request.get(\"status\"));"), "missing seeded evidence: order.status(request.get(\"status\"));")

        );
    }
}
