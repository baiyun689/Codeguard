package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-19-order-state-machine/transitionOrder.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class OrderStateMachine1OracleTest {
    @Test
    void transitionOrder_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-19-order-state-machine", "transitionOrder");
        assertEquals("已终止订单重新进入履约", result.observedFailure());
    }
}
