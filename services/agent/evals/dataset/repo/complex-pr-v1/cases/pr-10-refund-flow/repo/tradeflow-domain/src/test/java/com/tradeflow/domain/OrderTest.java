package com.tradeflow.domain;

import static org.junit.jupiter.api.Assertions.assertEquals;
import java.math.BigDecimal;
import org.junit.jupiter.api.Test;

final class OrderTest {
    @Test
    void status_change_preserves_tenant_and_amount() {
        Order order = new Order(
                "order-1", "tenant-a", new BigDecimal("19.90"), "CREATED");

        order.status("PAID");

        assertEquals("tenant-a", order.tenantId());
        assertEquals(new BigDecimal("19.90"), order.total());
        assertEquals("PAID", order.status());
    }
}
