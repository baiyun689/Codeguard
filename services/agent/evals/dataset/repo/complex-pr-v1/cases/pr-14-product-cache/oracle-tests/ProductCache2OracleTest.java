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
final class ProductCache2OracleTest {
    @Test
    @DisplayName("触发: 事务随后回滚且读请求重建缓存；后果: 缓存固化未提交状态或长期不一致")
    void updateProduct_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/ProductCacheService.java"));
        assertAll(
        () -> assertTrue(source.contains("String key = \"product:\" + context.tenantId() + \":\" + request.get(\"productId\");"), "missing seeded evidence: String key = \"product:\" + context.tenantId() + \":\" + request.get(\"productId\");"),
        () -> assertTrue(source.contains("cache.evict(key);"), "missing seeded evidence: cache.evict(key);"),
        () -> assertTrue(source.contains("events.publish(\"catalog.update\", request.get(\"productId\"), request.get(\"productJson\"));"), "missing seeded evidence: events.publish(\"catalog.update\", request.get(\"productId\"), request.get(\"productJson\"));")
        );
    }
}
