package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-16-notification-template/renderNotification.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class NotificationTemplate1OracleTest {
    @Test
    void renderNotification_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-16-notification-template", "renderNotification");
        assertEquals("读取服务数据或执行危险方法", result.observedFailure());
    }
}
