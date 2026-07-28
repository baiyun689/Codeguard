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
final class OrderSearch2OracleTest {
    @Test
    @DisplayName("触发: 提交极大页码；后果: 负 offset 绕过限制并触发大范围查询")
    void searchPage_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/OrderSearchService.java"));
        assertAll(
        () -> assertTrue(source.contains("int page = Integer.parseInt(request.get(\"page\"));"), "missing seeded evidence: int page = Integer.parseInt(request.get(\"page\"));"),
        () -> assertTrue(source.contains("int size = Integer.parseInt(request.get(\"size\"));"), "missing seeded evidence: int size = Integer.parseInt(request.get(\"size\"));"),
        () -> assertTrue(source.contains("int offset = page * size;"), "missing seeded evidence: int offset = page * size;")

        );
    }
}
