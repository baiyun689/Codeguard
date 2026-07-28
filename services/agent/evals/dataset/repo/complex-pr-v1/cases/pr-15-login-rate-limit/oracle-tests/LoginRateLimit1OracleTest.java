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
final class LoginRateLimit1OracleTest {
    @Test
    @DisplayName("触发: 客户端伪造请求头；后果: 绕过登录失败限流")
    void countByClientAddress_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/LoginRateLimitService.java"));
        assertAll(
        () -> assertTrue(source.contains("String clientIp = request.get(\"xForwardedFor\").split(\",\")[0].trim();"), "missing seeded evidence: String clientIp = request.get(\"xForwardedFor\").split(\",\")[0].trim();"),
        () -> assertTrue(source.contains("return cache.increment(\"login:\" + clientIp, Duration.ofMinutes(1));"), "missing seeded evidence: return cache.increment(\"login:\" + clientIp, Duration.ofMinutes(1));")
        );
    }
}
