package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-13-bulk-import/importRows.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class BulkImport2OracleTest {
    @Test
    void importRows_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-13-bulk-import", "importRows");
        assertEquals("调用方收到成功但数据仅部分导入", result.observedFailure());
    }
}
