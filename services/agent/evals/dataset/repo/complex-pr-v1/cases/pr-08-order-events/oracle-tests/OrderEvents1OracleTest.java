package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-08-order-events/changeOrderStatus.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class OrderEvents1OracleTest {
    @Test
    void changeOrderStatus_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-08-order-events", "changeOrderStatus");
        assertEquals("消费者观察到不存在的订单状态", result.observedFailure());
    }
}
