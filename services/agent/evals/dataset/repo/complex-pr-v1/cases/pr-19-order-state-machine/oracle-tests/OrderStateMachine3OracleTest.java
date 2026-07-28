package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-19-order-state-machine/publishCompensation.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class OrderStateMachine3OracleTest {
    @Test
    void publishCompensation_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-19-order-state-machine", "publishCompensation");
        assertEquals("补偿被当成后续新事件并覆盖正确状态", result.observedFailure());
    }
}
