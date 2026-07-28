package com.tradeflow.application.port;

import java.math.BigDecimal;

public interface PaymentGateway {
    String charge(String tenantId, String orderId, BigDecimal amount, String idempotencyKey);
    String refund(String paymentId, BigDecimal amount, String currency);
}
