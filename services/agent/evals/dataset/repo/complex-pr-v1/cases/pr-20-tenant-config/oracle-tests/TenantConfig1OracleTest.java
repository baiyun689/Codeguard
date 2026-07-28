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
final class TenantConfig1OracleTest {
    @Test
    @DisplayName("触发: 新租户尚未完成配置；后果: 租户获得共享高权限凭据")
    void loadTenantSecret_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/TenantConfigService.java"));
        assertAll(
        () -> assertTrue(source.contains("String tenantSecret = cache.get(\"config:\" + context.tenantId() + \":secret\")"), "missing seeded evidence: String tenantSecret = cache.get(\"config:\" + context.tenantId() + \":secret\")"),
        () -> assertTrue(source.contains(".orElse(request.get(\"globalSecret\"));"), "missing seeded evidence: .orElse(request.get(\"globalSecret\"));")

        );
    }
}
