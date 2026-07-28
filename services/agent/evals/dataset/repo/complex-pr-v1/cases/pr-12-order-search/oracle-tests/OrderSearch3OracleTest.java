package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-12-order-search/searchWithFallback.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class OrderSearch3OracleTest {
    @Test
    void searchWithFallback_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-12-order-search", "searchWithFallback");
        assertEquals("返回其他租户订单", result.observedFailure());
    }
}
