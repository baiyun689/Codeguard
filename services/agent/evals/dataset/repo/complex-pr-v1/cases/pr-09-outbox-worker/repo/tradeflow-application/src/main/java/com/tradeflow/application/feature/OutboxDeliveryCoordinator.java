package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class OutboxDeliveryCoordinator {
    private final OutboxDeliveryService service;
    private final AuditSink audit;
    private final TenantContext context;

    public OutboxDeliveryCoordinator(
            OutboxDeliveryService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object deliverReadyEvents(Map<String, String> request) {
        audit.record(context.tenantId(), "OUTBOX-WORKER", "deliverReadyEvents");
        return service.deliverReadyEvents(request);
    }

    public Object deliverOneEvent(Map<String, String> request) {
        audit.record(context.tenantId(), "OUTBOX-WORKER", "deliverOneEvent");
        return service.deliverOneEvent(request);
    }

    public Object scheduleRetry(Map<String, String> request) {
        audit.record(context.tenantId(), "OUTBOX-WORKER", "scheduleRetry");
        return service.scheduleRetry(request);
    }
}
