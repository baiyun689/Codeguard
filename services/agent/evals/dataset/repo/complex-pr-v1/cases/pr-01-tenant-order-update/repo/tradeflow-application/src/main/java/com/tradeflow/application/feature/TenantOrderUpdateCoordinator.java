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

    public Object tenantLookup(Map<String, String> request) {
        audit.record(context.tenantId(), "TENANT-ORDER-UPDATE", "tenantLookup");
        return service.tenantLookup(request);
    }

    public Object mutableProjection(Map<String, String> request) {
        audit.record(context.tenantId(), "TENANT-ORDER-UPDATE", "mutableProjection");
        return service.mutableProjection(request);
    }

    public Object prematureAudit(Map<String, String> request) {
        audit.record(context.tenantId(), "TENANT-ORDER-UPDATE", "prematureAudit");
        return service.prematureAudit(request);
    }
}
