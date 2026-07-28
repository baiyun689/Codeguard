package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-08-order-events/applyVersionedEvent.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class OrderEvents3OracleTest {
    @Test
    void applyVersionedEvent_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-08-order-events", "applyVersionedEvent");
        assertEquals("订单回退到旧状态", result.observedFailure());
    }
}
