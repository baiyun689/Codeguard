package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class OrderEventsCoordinator {
    private final OrderEventsService service;
    private final AuditSink audit;
    private final TenantContext context;

    public OrderEventsCoordinator(
            OrderEventsService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object changeOrderStatus(Map<String, String> request) {
        audit.record(context.tenantId(), "ORDER-EVENTS", "changeOrderStatus");
        return service.changeOrderStatus(request);
    }

    public Object consumeOrderEvent(Map<String, String> request) {
        audit.record(context.tenantId(), "ORDER-EVENTS", "consumeOrderEvent");
        return service.consumeOrderEvent(request);
    }

    public Object applyVersionedEvent(Map<String, String> request) {
        audit.record(context.tenantId(), "ORDER-EVENTS", "applyVersionedEvent");
        return service.applyVersionedEvent(request);
    }
}
