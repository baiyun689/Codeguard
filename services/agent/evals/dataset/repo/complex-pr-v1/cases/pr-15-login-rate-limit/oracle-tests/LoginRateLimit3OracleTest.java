package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-15-login-rate-limit/checkRateLimit.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class LoginRateLimit3OracleTest {
    @Test
    void checkRateLimit_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-15-login-rate-limit", "checkRateLimit");
        assertEquals("敏感登录入口失去暴力破解保护", result.observedFailure());
    }
}
