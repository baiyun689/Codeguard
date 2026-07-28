package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-16-notification-template/unsubscribeRecipient.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class NotificationTemplate2OracleTest {
    @Test
    void unsubscribeRecipient_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-16-notification-template", "unsubscribeRecipient");
        assertEquals("取消他人通知订阅", result.observedFailure());
    }
}
