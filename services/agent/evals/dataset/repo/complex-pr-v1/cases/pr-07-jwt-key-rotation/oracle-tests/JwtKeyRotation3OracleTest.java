package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-07-jwt-key-rotation/loadTenantRoles.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class JwtKeyRotation3OracleTest {
    @Test
    void loadTenantRoles_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-07-jwt-key-rotation", "loadTenantRoles");
        assertEquals("低权限租户继承另一租户角色", result.observedFailure());
    }
}
