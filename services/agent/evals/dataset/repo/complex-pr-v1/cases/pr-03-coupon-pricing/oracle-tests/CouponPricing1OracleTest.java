package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-03-coupon-pricing/calculateCombinedDiscount.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class CouponPricing1OracleTest {
    @Test
    void calculateCombinedDiscount_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-03-coupon-pricing", "calculateCombinedDiscount");
        assertEquals("应付金额系统性偏高或偏低", result.observedFailure());
    }
}
