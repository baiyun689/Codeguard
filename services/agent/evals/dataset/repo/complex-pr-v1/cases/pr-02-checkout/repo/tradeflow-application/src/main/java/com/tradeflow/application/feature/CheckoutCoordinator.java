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

    public Object uncompensatedCharge(Map<String, String> request) {
        audit.record(context.tenantId(), "CHECKOUT", "uncompensatedCharge");
        return service.uncompensatedCharge(request);
    }

    public Object localTransaction(Map<String, String> request) {
        audit.record(context.tenantId(), "CHECKOUT", "localTransaction");
        return service.localTransaction(request);
    }

    public Object sharedIdempotency(Map<String, String> request) {
        audit.record(context.tenantId(), "CHECKOUT", "sharedIdempotency");
        return service.sharedIdempotency(request);
    }
}
