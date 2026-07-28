package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-16-notification-template/sendNotification.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class NotificationTemplate3OracleTest {
    @Test
    void sendNotification_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-16-notification-template", "sendNotification");
        assertEquals("用户收到重复邮件或短信", result.observedFailure());
    }
}
