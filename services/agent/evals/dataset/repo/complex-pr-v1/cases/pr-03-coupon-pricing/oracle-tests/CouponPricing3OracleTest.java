package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-03-coupon-pricing/loadCustomerPrice.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class CouponPricing3OracleTest {
    @Test
    void loadCustomerPrice_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-03-coupon-pricing", "loadCustomerPrice");
        assertEquals("后一个客户读取前一个等级价格", result.observedFailure());
    }
}
