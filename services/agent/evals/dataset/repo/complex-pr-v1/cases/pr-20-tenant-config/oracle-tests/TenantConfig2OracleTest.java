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
final class TenantConfig2OracleTest {
    @Test
    @DisplayName("触发: 热更新与请求读取交错；后果: 读取到混合版本或抛出并发异常")
    void reloadConfiguration_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/TenantConfigService.java"));
        assertAll(
        () -> assertTrue(source.contains("runtimeConfig.clear();"), "missing seeded evidence: runtimeConfig.clear();"),
        () -> assertTrue(source.contains("runtimeConfig.putAll(request);"), "missing seeded evidence: runtimeConfig.putAll(request);"),
        () -> assertTrue(source.contains("return runtimeConfig.size();"), "missing seeded evidence: return runtimeConfig.size();")

        );
    }
}
