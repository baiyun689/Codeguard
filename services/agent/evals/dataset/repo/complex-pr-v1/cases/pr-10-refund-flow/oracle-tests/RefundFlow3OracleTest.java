package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-10-refund-flow/refundConvertedAmount.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class RefundFlow3OracleTest {
    @Test
    void refundConvertedAmount_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-10-refund-flow", "refundConvertedAmount");
        assertEquals("账本与渠道金额产生不可对账差异", result.observedFailure());
    }
}
