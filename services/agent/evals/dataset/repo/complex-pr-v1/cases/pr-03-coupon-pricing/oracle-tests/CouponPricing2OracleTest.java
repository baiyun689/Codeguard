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
final class CouponPricing2OracleTest {
    @Test
    @DisplayName("触发: 券同时匹配会员与活动规则；后果: 同一优惠被扣减两次")
    void applyCouponRules_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/CouponPricingService.java"));
        assertAll(
        () -> assertTrue(source.contains("BigDecimal total = new BigDecimal(request.get(\"subtotal\"));"), "missing seeded evidence: BigDecimal total = new BigDecimal(request.get(\"subtotal\"));"),
        () -> assertTrue(source.contains("total = total.subtract(new BigDecimal(request.get(\"campaignCoupon\")));"), "missing seeded evidence: total = total.subtract(new BigDecimal(request.get(\"campaignCoupon\")));"),
        () -> assertTrue(source.contains("return total.subtract(new BigDecimal(request.get(\"memberCoupon\")));"), "missing seeded evidence: return total.subtract(new BigDecimal(request.get(\"memberCoupon\")));")

        );
    }
}
