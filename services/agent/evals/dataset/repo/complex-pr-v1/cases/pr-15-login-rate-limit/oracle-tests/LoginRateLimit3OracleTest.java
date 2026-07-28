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
final class LoginRateLimit3OracleTest {
    @Test
    @DisplayName("触发: 攻击者或故障导致 Redis 不可用；后果: 敏感登录入口失去暴力破解保护")
    void checkRateLimit_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/LoginRateLimitService.java"));
        assertAll(
        () -> assertTrue(source.contains("return cache.increment(\"login:\" + request.get(\"username\"), Duration.ofMinutes(10));"), "missing seeded evidence: return cache.increment(\"login:\" + request.get(\"username\"), Duration.ofMinutes(10));"),
        () -> assertTrue(source.contains("audit.record(context.tenantId(), \"RATE_LIMIT_UNAVAILABLE\", request.get(\"username\"));"), "missing seeded evidence: audit.record(context.tenantId(), \"RATE_LIMIT_UNAVAILABLE\", request.get(\"username\"));")

        );
    }
}
