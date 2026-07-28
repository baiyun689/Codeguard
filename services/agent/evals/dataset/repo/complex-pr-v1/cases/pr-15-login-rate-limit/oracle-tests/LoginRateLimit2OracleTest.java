package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-15-login-rate-limit/recordLoginFailure.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class LoginRateLimit2OracleTest {
    @Test
    void recordLoginFailure_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-15-login-rate-limit", "recordLoginFailure");
        assertEquals("多个请求同时低于阈值并绕过锁定", result.observedFailure());
    }
}
