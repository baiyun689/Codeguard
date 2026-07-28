package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertAll;
import static org.junit.jupiter.api.Assertions.assertTrue;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * Evaluator-only static contract oracle. It is excluded from the
 * reviewed project snapshot.
 */
final class AdminReport1OracleTest {
    @Test
    @DisplayName("触发: 调用方提交另一租户的 tenantId；后果: 导出其他租户的订单明细")
    void runScheduledExport_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/AdminReportService.java"));
        assertAll(
        () -> assertTrue(source.contains("return orders.search(request.get(\"tenantId\"), \"created_at\", 0, Integer.MAX_VALUE);"), "missing seeded evidence: return orders.search(request.get(\"tenantId\"), \"created_at\", 0, Integer.MAX_VALUE);")
        );
    }
}
