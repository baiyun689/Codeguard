package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class TenantConfigCoordinator {
    private final TenantConfigService service;
    private final AuditSink audit;
    private final TenantContext context;

    public TenantConfigCoordinator(
            TenantConfigService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object loadTenantSecret(Map<String, String> request) {
        audit.record(context.tenantId(), "TENANT-CONFIG", "loadTenantSecret");
        return service.loadTenantSecret(request);
    }

    public Object reloadConfiguration(Map<String, String> request) {
        audit.record(context.tenantId(), "TENANT-CONFIG", "reloadConfiguration");
        return service.reloadConfiguration(request);
    }

    public Object publishConfiguration(Map<String, String> request) {
        audit.record(context.tenantId(), "TENANT-CONFIG", "publishConfiguration");
        return service.publishConfiguration(request);
    }
}
