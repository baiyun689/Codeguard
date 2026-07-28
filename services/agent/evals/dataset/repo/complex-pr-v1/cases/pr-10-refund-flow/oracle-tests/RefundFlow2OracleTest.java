package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-10-refund-flow/refundRemainingAmount.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class RefundFlow2OracleTest {
    @Test
    void refundRemainingAmount_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-10-refund-flow", "refundRemainingAmount");
        assertEquals("重复退款造成资金损失", result.observedFailure());
    }
}
