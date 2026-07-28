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
final class AdminReport2OracleTest {
    @Test
    @DisplayName("触发: 大租户导出多年数据；后果: 堆内存耗尽并影响在线请求")
    void renderFullReport_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/AdminReportService.java"));
        assertAll(
        () -> assertTrue(source.contains("List<Order> all = orders.search(context.tenantId(), \"created_at\", 0, Integer.MAX_VALUE);"), "missing seeded evidence: List<Order> all = orders.search(context.tenantId(), \"created_at\", 0, Integer.MAX_VALUE);")
        );
    }
}
