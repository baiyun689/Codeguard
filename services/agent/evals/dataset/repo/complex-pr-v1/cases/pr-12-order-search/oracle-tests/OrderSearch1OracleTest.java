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
final class OrderSearch1OracleTest {
    @Test
    @DisplayName("触发: 调用方提交 SQL 片段作为排序字段；后果: 查询语义被篡改或数据泄露")
    void searchWithSort_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/OrderSearchService.java"));
        assertAll(
        () -> assertTrue(source.contains("String expression = \"order by \" + request.get(\"sort\");"), "missing seeded evidence: String expression = \"order by \" + request.get(\"sort\");"),
        () -> assertTrue(source.contains("return orders.search(context.tenantId(), expression, 0, 100);"), "missing seeded evidence: return orders.search(context.tenantId(), expression, 0, 100);")

        );
    }
}
