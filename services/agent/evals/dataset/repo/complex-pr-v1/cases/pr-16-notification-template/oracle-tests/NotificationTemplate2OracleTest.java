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
final class NotificationTemplate2OracleTest {
    @Test
    @DisplayName("触发: 构造或泄露另一个租户的订阅 ID；后果: 取消他人通知订阅")
    void unsubscribeRecipient_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/NotificationTemplateService.java"));
        assertAll(
        () -> assertTrue(source.contains("UserAccount account = users.findById(request.get(\"subscriptionId\")).orElseThrow();"), "missing seeded evidence: UserAccount account = users.findById(request.get(\"subscriptionId\")).orElseThrow();"),
        () -> assertTrue(source.contains("users.save(new UserAccount(account.id(), account.tenantId(), Set.of(), account.version()));"), "missing seeded evidence: users.save(new UserAccount(account.id(), account.tenantId(), Set.of(), account.version()));")
        );
    }
}
