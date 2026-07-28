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
final class CouponPricing1OracleTest {
    @Test
    @DisplayName("触发: 订单同时使用满减券和会员折扣；后果: 应付金额系统性偏高或偏低")
    void calculateCombinedDiscount_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/CouponPricingService.java"));
        assertAll(
        () -> assertTrue(source.contains("BigDecimal subtotal = new BigDecimal(request.get(\"subtotal\"));"), "missing seeded evidence: BigDecimal subtotal = new BigDecimal(request.get(\"subtotal\"));"),
        () -> assertTrue(source.contains("BigDecimal percent = new BigDecimal(request.get(\"percent\"));"), "missing seeded evidence: BigDecimal percent = new BigDecimal(request.get(\"percent\"));"),
        () -> assertTrue(source.contains("BigDecimal fixed = new BigDecimal(request.get(\"fixed\"));"), "missing seeded evidence: BigDecimal fixed = new BigDecimal(request.get(\"fixed\"));")
        );
    }
}
