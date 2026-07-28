package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-09-outbox-worker/deliverReadyEvents.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class OutboxDelivery1OracleTest {
    @Test
    void deliverReadyEvents_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-09-outbox-worker", "deliverReadyEvents");
        assertEquals("相同业务事件重复投递", result.observedFailure());
    }
}
