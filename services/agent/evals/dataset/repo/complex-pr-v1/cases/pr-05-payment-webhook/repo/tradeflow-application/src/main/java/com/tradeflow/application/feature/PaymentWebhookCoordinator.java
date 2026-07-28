package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class PaymentWebhookCoordinator {
    private final PaymentWebhookService service;
    private final AuditSink audit;
    private final TenantContext context;

    public PaymentWebhookCoordinator(
            PaymentWebhookService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object verifyWebhookSignature(Map<String, String> request) {
        audit.record(context.tenantId(), "PAYMENT-WEBHOOK", "verifyWebhookSignature");
        return service.verifyWebhookSignature(request);
    }

    public Object acceptWebhookEvent(Map<String, String> request) {
        audit.record(context.tenantId(), "PAYMENT-WEBHOOK", "acceptWebhookEvent");
        return service.acceptWebhookEvent(request);
    }

    public Object applyPaymentEvent(Map<String, String> request) {
        audit.record(context.tenantId(), "PAYMENT-WEBHOOK", "applyPaymentEvent");
        return service.applyPaymentEvent(request);
    }
}
