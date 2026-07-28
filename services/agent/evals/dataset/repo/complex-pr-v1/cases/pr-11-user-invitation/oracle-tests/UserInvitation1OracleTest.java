package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-11-user-invitation/inviteTenantMember.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class UserInvitation1OracleTest {
    @Test
    void inviteTenantMember_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-11-user-invitation", "inviteTenantMember");
        assertEquals("跨租户创建或修改成员", result.observedFailure());
    }
}
