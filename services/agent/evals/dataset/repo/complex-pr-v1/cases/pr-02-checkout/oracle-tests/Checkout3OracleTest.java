package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-02-checkout/sharedIdempotency.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class Checkout3OracleTest {
    @Test
    void sharedIdempotency_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-02-checkout", "sharedIdempotency");
        assertEquals("一个租户收到另一租户的支付结果或请求被错误抑制", result.observedFailure());
    }
}
