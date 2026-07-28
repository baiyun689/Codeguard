package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-11-user-invitation/sendInvitation.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class UserInvitation3OracleTest {
    @Test
    void sendInvitation_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-11-user-invitation", "sendInvitation");
        assertEquals("收件人得到无法兑换的幽灵邀请", result.observedFailure());
    }
}
