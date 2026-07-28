package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-07-jwt-key-rotation/isTokenActive.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class JwtKeyRotation2OracleTest {
    @Test
    void isTokenActive_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-07-jwt-key-rotation", "isTokenActive");
        assertEquals("凭据有效期被错误延长或全部被拒绝", result.observedFailure());
    }
}
