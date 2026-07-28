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
final class AdminReport3OracleTest {
    @Test
    @DisplayName("触发: 订单备注以公式前缀开头；后果: 管理员打开报表时执行公式")
    void renderReportRow_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/AdminReportService.java"));
        assertAll(
        () -> assertTrue(source.contains("return request.get(\"orderId\") + \",\" + request.get(\"customerNote\") + System.lineSeparator();"), "missing seeded evidence: return request.get(\"orderId\") + \",\" + request.get(\"customerNote\") + System.lineSeparator();")
        );
    }
}
