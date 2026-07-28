package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class TenantOrderUpdateCoordinator {
    private final TenantOrderUpdateService service;
    private final AuditSink audit;
    private final TenantContext context;

    public TenantOrderUpdateCoordinator(
            TenantOrderUpdateService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object updateOrder(Map<String, String> request) {
        audit.record(context.tenantId(), "TENANT-ORDER-UPDATE", "updateOrder");
        return service.updateOrder(request);
    }

    public Object applyOrderChanges(Map<String, String> request) {
        audit.record(context.tenantId(), "TENANT-ORDER-UPDATE", "applyOrderChanges");
        return service.applyOrderChanges(request);
    }

    public Object recordOrderUpdate(Map<String, String> request) {
        audit.record(context.tenantId(), "TENANT-ORDER-UPDATE", "recordOrderUpdate");
        return service.recordOrderUpdate(request);
    }
}
