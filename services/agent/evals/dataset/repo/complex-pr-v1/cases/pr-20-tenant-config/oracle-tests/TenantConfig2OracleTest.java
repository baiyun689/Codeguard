package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-20-tenant-config/reloadConfiguration.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class TenantConfig2OracleTest {
    @Test
    void reloadConfiguration_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-20-tenant-config", "reloadConfiguration");
        assertEquals("读取到混合版本或抛出并发异常", result.observedFailure());
    }
}
