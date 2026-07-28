package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-07-jwt-key-rotation/loadSigningKey.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class JwtKeyRotation1OracleTest {
    @Test
    void loadSigningKey_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-07-jwt-key-rotation", "loadSigningKey");
        assertEquals("服务访问内网或云元数据地址", result.observedFailure());
    }
}
