package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-03-coupon-pricing/applyCouponRules.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class CouponPricing2OracleTest {
    @Test
    void applyCouponRules_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-03-coupon-pricing", "applyCouponRules");
        assertEquals("同一优惠被扣减两次", result.observedFailure());
    }
}
