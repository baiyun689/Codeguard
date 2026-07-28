package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-14-product-cache/createProduct.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class ProductCache3OracleTest {
    @Test
    void createProduct_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-14-product-cache", "createProduct");
        assertEquals("有效商品在 TTL 内持续显示不存在", result.observedFailure());
    }
}
