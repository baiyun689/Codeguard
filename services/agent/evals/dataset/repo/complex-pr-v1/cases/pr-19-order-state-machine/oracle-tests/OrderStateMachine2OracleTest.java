package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-19-order-state-machine/mapOrderUpdate.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class OrderStateMachine2OracleTest {
    @Test
    void mapOrderUpdate_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-19-order-state-machine", "mapOrderUpdate");
        assertEquals("覆盖另一请求已提交的状态", result.observedFailure());
    }
}
