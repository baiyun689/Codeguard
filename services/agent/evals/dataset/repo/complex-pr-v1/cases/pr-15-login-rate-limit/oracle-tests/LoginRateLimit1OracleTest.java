package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-15-login-rate-limit/countByClientAddress.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class LoginRateLimit1OracleTest {
    @Test
    void countByClientAddress_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-15-login-rate-limit", "countByClientAddress");
        assertEquals("绕过登录失败限流", result.observedFailure());
    }
}
