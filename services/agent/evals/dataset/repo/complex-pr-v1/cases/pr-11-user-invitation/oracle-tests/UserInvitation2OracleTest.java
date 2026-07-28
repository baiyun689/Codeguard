package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-11-user-invitation/inviteWithRoleCheck.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class UserInvitation2OracleTest {
    @Test
    void inviteWithRoleCheck_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-11-user-invitation", "inviteWithRoleCheck");
        assertEquals("已撤权主体仍能创建高权限邀请", result.observedFailure());
    }
}
