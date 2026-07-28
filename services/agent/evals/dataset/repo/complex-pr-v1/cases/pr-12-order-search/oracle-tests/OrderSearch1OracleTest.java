package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-12-order-search/searchWithSort.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class OrderSearch1OracleTest {
    @Test
    void searchWithSort_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-12-order-search", "searchWithSort");
        assertEquals("查询语义被篡改或数据泄露", result.observedFailure());
    }
}
