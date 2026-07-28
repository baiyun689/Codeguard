package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-08-order-events/consumeOrderEvent.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class OrderEvents2OracleTest {
    @Test
    void consumeOrderEvent_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-08-order-events", "consumeOrderEvent");
        assertEquals("消息 offset 前进且事件永久丢失", result.observedFailure());
    }
}
