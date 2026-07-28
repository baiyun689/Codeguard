package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-02-checkout/localTransaction.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class Checkout2OracleTest {
    @Test
    void localTransaction_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-02-checkout", "localTransaction");
        assertEquals("预期原子更新可能部分提交", result.observedFailure());
    }
}
