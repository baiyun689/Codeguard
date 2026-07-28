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
final class RefundFlow3OracleTest {
    @Test
    @DisplayName("触发: 非整数汇率和小数金额退款；后果: 账本与渠道金额产生不可对账差异")
    void refundConvertedAmount_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/RefundFlowService.java"));
        assertAll(
        () -> assertTrue(source.contains("BigDecimal source = new BigDecimal(request.get(\"amount\"));"), "missing seeded evidence: BigDecimal source = new BigDecimal(request.get(\"amount\"));"),
        () -> assertTrue(source.contains("BigDecimal rate = new BigDecimal(request.get(\"rate\"));"), "missing seeded evidence: BigDecimal rate = new BigDecimal(request.get(\"rate\"));"),
        () -> assertTrue(source.contains("BigDecimal gateway = ledger.setScale(Integer.parseInt(request.get(\"minorUnits\")), RoundingMode.HALF_UP);"), "missing seeded evidence: BigDecimal gateway = ledger.setScale(Integer.parseInt(request.get(\"minorUnits\")), RoundingMode.HALF_UP);")
        );
    }
}
