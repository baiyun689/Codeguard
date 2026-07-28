package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-09-outbox-worker/scheduleRetry.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class OutboxDelivery3OracleTest {
    @Test
    void scheduleRetry_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-09-outbox-worker", "scheduleRetry");
        assertEquals("形成高频重试并压垮依赖", result.observedFailure());
    }
}
