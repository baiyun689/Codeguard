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
final class UserInvitation2OracleTest {
    @Test
    @DisplayName("触发: 管理员权限在并发请求中被撤销；后果: 已撤权主体仍能创建高权限邀请")
    void inviteWithRoleCheck_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/UserInvitationService.java"));
        assertAll(
        () -> assertTrue(source.contains("UserAccount operator = users.findByTenantAndId(context.tenantId(), context.userId()).orElseThrow();"), "missing seeded evidence: UserAccount operator = users.findByTenantAndId(context.tenantId(), context.userId()).orElseThrow();"),
        () -> assertTrue(source.contains("if (!operator.hasRole(\"ADMIN\")) throw new SecurityException(\"forbidden\");"), "missing seeded evidence: if (!operator.hasRole(\"ADMIN\")) throw new SecurityException(\"forbidden\");"),
        () -> assertTrue(source.contains("UserAccount invited = new UserAccount(request.get(\"userId\"), context.tenantId(), Set.of(request.get(\"role\")), 0);"), "missing seeded evidence: UserAccount invited = new UserAccount(request.get(\"userId\"), context.tenantId(), Set.of(request.get(\"role\")), 0);")

        );
    }
}
