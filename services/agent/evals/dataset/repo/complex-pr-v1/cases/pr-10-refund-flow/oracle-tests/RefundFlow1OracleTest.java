package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-10-refund-flow/refundAgainstOrder.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class RefundFlow1OracleTest {
    @Test
    void refundAgainstOrder_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-10-refund-flow", "refundAgainstOrder");
        assertEquals("累计退款超过实际支付金额", result.observedFailure());
    }
}
