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
        final class Checkout2OracleTest {
            @Test
            @DisplayName("触发: 后续持久化步骤抛出运行时异常；后果: 预期原子更新可能部分提交")
            void localTransaction_seed_is_present() throws Exception {
                Path repo = Path.of(System.getProperty("tradeflow.repo"));
                String source = Files.readString(repo.resolve(
                        "tradeflow-application/src/main/java/com/tradeflow/application/feature/CheckoutService.java"));
                assertAll(
                () -> assertTrue(source.contains("return persistCheckout(request);"), "missing seeded evidence: return persistCheckout(request);"),
                () -> assertTrue(source.contains("Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"), "missing seeded evidence: Order order = orders.findByTenantAndId(context.tenantId(), request.get(\"orderId\")).orElseThrow();"),
                () -> assertTrue(source.contains("orders.save(order);"), "missing seeded evidence: orders.save(order);"),
                () -> assertTrue(source.contains("outbox.save(new OutboxEvent("), "missing seeded evidence: outbox.save(new OutboxEvent(")
,
                () -> assertTrue(source.indexOf("orders.save(order);") < source.indexOf("outbox.save(new OutboxEvent("), "seeded side-effect order changed")
                );
            }
        }
