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
final class OrderSearch3OracleTest {
    @Test
    @DisplayName("触发: 当前租户没有匹配记录；后果: 返回其他租户订单")
    void searchWithFallback_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/OrderSearchService.java"));
        assertAll(
        () -> assertTrue(source.contains("List<Order> scoped = orders.search(context.tenantId(), request.get(\"query\"), 0, 50);"), "missing seeded evidence: List<Order> scoped = orders.search(context.tenantId(), request.get(\"query\"), 0, 50);"),
        () -> assertTrue(source.contains("return scoped.isEmpty() ? orders.search(\"\", request.get(\"query\"), 0, 50) : scoped;"), "missing seeded evidence: return scoped.isEmpty() ? orders.search(\"\", request.get(\"query\"), 0, 50) : scoped;")

        );
    }
}
