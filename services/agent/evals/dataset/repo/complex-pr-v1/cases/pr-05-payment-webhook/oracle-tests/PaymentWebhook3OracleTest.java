package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-05-payment-webhook/applyPaymentEvent.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class PaymentWebhook3OracleTest {
    @Test
    void applyPaymentEvent_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-05-payment-webhook", "applyPaymentEvent");
        assertEquals("重试被完成标记挡住，支付状态永久丢失", result.observedFailure());
    }
}
