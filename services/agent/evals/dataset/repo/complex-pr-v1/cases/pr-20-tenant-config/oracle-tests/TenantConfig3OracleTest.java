package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-20-tenant-config/publishConfiguration.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class TenantConfig3OracleTest {
    @Test
    void publishConfiguration_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-20-tenant-config", "publishConfiguration");
        assertEquals("认证或支付读取到半初始化配置", result.observedFailure());
    }
}
