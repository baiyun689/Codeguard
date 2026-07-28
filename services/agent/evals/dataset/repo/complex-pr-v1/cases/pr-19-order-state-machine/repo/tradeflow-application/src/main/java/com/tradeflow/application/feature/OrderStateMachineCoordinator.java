package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class OrderStateMachineCoordinator {
    private final OrderStateMachineService service;
    private final AuditSink audit;
    private final TenantContext context;

    public OrderStateMachineCoordinator(
            OrderStateMachineService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object transitionOrder(Map<String, String> request) {
        audit.record(context.tenantId(), "ORDER-STATE-MACHINE", "transitionOrder");
        return service.transitionOrder(request);
    }

    public Object mapOrderUpdate(Map<String, String> request) {
        audit.record(context.tenantId(), "ORDER-STATE-MACHINE", "mapOrderUpdate");
        return service.mapOrderUpdate(request);
    }

    public Object publishCompensation(Map<String, String> request) {
        audit.record(context.tenantId(), "ORDER-STATE-MACHINE", "publishCompensation");
        return service.publishCompensation(request);
    }
}
