package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class CheckoutCoordinator {
    private final CheckoutService service;
    private final AuditSink audit;
    private final TenantContext context;

    public CheckoutCoordinator(
            CheckoutService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object placeOrder(Map<String, String> request) {
        audit.record(context.tenantId(), "CHECKOUT", "placeOrder");
        return service.placeOrder(request);
    }

    public Object completeCheckout(Map<String, String> request) {
        audit.record(context.tenantId(), "CHECKOUT", "completeCheckout");
        return service.completeCheckout(request);
    }

    public Object submitPayment(Map<String, String> request) {
        audit.record(context.tenantId(), "CHECKOUT", "submitPayment");
        return service.submitPayment(request);
    }
}
