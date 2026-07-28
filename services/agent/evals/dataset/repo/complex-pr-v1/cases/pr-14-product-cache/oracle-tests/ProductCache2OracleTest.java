package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-14-product-cache/updateProduct.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class ProductCache2OracleTest {
    @Test
    void updateProduct_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-14-product-cache", "updateProduct");
        assertEquals("缓存固化未提交状态或长期不一致", result.observedFailure());
    }
}
