package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-02-checkout/uncompensatedCharge.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class Checkout1OracleTest {
    @Test
    void uncompensatedCharge_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-02-checkout", "uncompensatedCharge");
        assertEquals("用户被扣款但订单无法履约", result.observedFailure());
    }
}
