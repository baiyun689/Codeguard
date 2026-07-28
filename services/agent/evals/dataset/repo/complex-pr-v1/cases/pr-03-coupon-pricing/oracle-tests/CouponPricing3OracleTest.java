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
final class CouponPricing3OracleTest {
    @Test
    @DisplayName("触发: 不同等级客户依次查询同一商品；后果: 后一个客户读取前一个等级价格")
    void loadCustomerPrice_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/CouponPricingService.java"));
        assertAll(
        () -> assertTrue(source.contains("String key = \"price:\" + context.tenantId() + \":\" + request.get(\"productId\");"), "missing seeded evidence: String key = \"price:\" + context.tenantId() + \":\" + request.get(\"productId\");"),
        () -> assertTrue(source.contains("return cache.get(key).orElseGet(() -> {"), "missing seeded evidence: return cache.get(key).orElseGet(() -> {"),
        () -> assertTrue(source.contains("String price = request.get(\"calculatedPrice\");"), "missing seeded evidence: String price = request.get(\"calculatedPrice\");")

        );
    }
}
