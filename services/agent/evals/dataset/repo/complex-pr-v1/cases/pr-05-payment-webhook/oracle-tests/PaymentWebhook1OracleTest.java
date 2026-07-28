package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-05-payment-webhook/verifyWebhookSignature.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class PaymentWebhook1OracleTest {
    @Test
    void verifyWebhookSignature_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-05-payment-webhook", "verifyWebhookSignature");
        assertEquals("合法回调被拒绝或错误实现下可绕过认证", result.observedFailure());
    }
}
