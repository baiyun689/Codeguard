package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-12-order-search/searchPage.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class OrderSearch2OracleTest {
    @Test
    void searchPage_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-12-order-search", "searchPage");
        assertEquals("负 offset 绕过限制并触发大范围查询", result.observedFailure());
    }
}
