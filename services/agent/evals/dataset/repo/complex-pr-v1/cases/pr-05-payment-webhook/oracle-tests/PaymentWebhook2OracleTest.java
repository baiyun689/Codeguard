package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-05-payment-webhook/acceptWebhookEvent.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class PaymentWebhook2OracleTest {
    @Test
    void acceptWebhookEvent_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-05-payment-webhook", "acceptWebhookEvent");
        assertEquals("订单状态或退款副作用重复执行", result.observedFailure());
    }
}
