package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-01-tenant-order-update/tenantLookup.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class TenantOrderUpdate1OracleTest {
    @Test
    void tenantLookup_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-01-tenant-order-update", "tenantLookup");
        assertEquals("跨租户订单状态被修改", result.observedFailure());
    }
}
