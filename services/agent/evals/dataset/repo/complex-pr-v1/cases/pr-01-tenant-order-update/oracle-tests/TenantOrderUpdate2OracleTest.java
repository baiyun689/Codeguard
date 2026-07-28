package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-01-tenant-order-update/mutableProjection.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class TenantOrderUpdate2OracleTest {
    @Test
    void mutableProjection_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-01-tenant-order-update", "mutableProjection");
        assertEquals("订单金额或状态机被绕过", result.observedFailure());
    }
}
