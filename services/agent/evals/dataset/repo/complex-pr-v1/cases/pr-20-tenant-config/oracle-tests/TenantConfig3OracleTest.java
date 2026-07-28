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
final class TenantConfig3OracleTest {
    @Test
    @DisplayName("触发: 请求恰好落在热更新窗口；后果: 认证或支付读取到半初始化配置")
    void publishConfiguration_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/TenantConfigService.java"));
        assertAll(
        () -> assertTrue(source.contains("runtimeConfig.put(\"tenant\", context.tenantId());"), "missing seeded evidence: runtimeConfig.put(\"tenant\", context.tenantId());"),
        () -> assertTrue(source.contains("runtimeConfig.put(\"paymentUrl\", request.get(\"paymentUrl\"));"), "missing seeded evidence: runtimeConfig.put(\"paymentUrl\", request.get(\"paymentUrl\"));"),
        () -> assertTrue(source.contains("runtimeConfig.put(\"secret\", request.get(\"secret\"));"), "missing seeded evidence: runtimeConfig.put(\"secret\", request.get(\"secret\"));")

        );
    }
}
