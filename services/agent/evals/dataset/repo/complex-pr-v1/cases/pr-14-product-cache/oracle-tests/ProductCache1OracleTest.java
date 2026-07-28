package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-14-product-cache/loadProduct.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class ProductCache1OracleTest {
    @Test
    void loadProduct_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-14-product-cache", "loadProduct");
        assertEquals("读取另一租户价格或配置", result.observedFailure());
    }
}
