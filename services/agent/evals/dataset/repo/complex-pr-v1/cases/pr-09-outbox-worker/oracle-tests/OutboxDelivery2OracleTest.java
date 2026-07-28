package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-09-outbox-worker/deliverOneEvent.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class OutboxDelivery2OracleTest {
    @Test
    void deliverOneEvent_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-09-outbox-worker", "deliverOneEvent");
        assertEquals("事件被永久标记完成但实际未送达", result.observedFailure());
    }
}
