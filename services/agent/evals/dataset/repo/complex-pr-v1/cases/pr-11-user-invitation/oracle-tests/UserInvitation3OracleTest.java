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
final class UserInvitation3OracleTest {
    @Test
    @DisplayName("触发: 数据库保存失败；后果: 收件人得到无法兑换的幽灵邀请")
    void sendInvitation_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/UserInvitationService.java"));
        assertAll(
        () -> assertTrue(source.contains("events.publish(\"email.invitation\", request.get(\"email\"), request.get(\"token\"));"), "missing seeded evidence: events.publish(\"email.invitation\", request.get(\"email\"), request.get(\"token\"));"),
        () -> assertTrue(source.contains("UserAccount invited = new UserAccount(request.get(\"userId\"), context.tenantId(), Set.of(\"MEMBER\"), 0);"), "missing seeded evidence: UserAccount invited = new UserAccount(request.get(\"userId\"), context.tenantId(), Set.of(\"MEMBER\"), 0);"),
        () -> assertTrue(source.contains("users.save(invited);"), "missing seeded evidence: users.save(invited);")

        );
    }
}
