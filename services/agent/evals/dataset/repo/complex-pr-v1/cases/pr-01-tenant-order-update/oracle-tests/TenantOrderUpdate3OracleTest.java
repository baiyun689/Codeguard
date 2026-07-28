package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-01-tenant-order-update/prematureAudit.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class TenantOrderUpdate3OracleTest {
    @Test
    void prematureAudit_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-01-tenant-order-update", "prematureAudit");
        assertEquals("审计系统记录不存在的成功操作", result.observedFailure());
    }
}
