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
final class ProductCache3OracleTest {
    @Test
    @DisplayName("触发: 先查询不存在商品再立即创建；后果: 有效商品在 TTL 内持续显示不存在")
    void createProduct_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/ProductCacheService.java"));
        assertAll(
        () -> assertTrue(source.contains("String key = \"product:\" + context.tenantId() + \":\" + request.get(\"productId\");"), "missing seeded evidence: String key = \"product:\" + context.tenantId() + \":\" + request.get(\"productId\");"),
        () -> assertTrue(source.contains("events.publish(\"catalog.created\", request.get(\"productId\"), request.get(\"productJson\"));"), "missing seeded evidence: events.publish(\"catalog.created\", request.get(\"productId\"), request.get(\"productJson\"));"),
        () -> assertTrue(source.contains("return cache.get(key).orElse(\"NOT_FOUND\");"), "missing seeded evidence: return cache.get(key).orElse(\"NOT_FOUND\");")

        );
    }
}
