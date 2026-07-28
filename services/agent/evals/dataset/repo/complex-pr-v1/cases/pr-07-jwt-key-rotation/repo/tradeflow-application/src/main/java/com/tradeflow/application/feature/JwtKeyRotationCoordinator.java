package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class JwtKeyRotationCoordinator {
    private final JwtKeyRotationService service;
    private final AuditSink audit;
    private final TenantContext context;

    public JwtKeyRotationCoordinator(
            JwtKeyRotationService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object loadSigningKey(Map<String, String> request) {
        audit.record(context.tenantId(), "JWT-KEY-ROTATION", "loadSigningKey");
        return service.loadSigningKey(request);
    }

    public Object isTokenActive(Map<String, String> request) {
        audit.record(context.tenantId(), "JWT-KEY-ROTATION", "isTokenActive");
        return service.isTokenActive(request);
    }

    public Object loadTenantRoles(Map<String, String> request) {
        audit.record(context.tenantId(), "JWT-KEY-ROTATION", "loadTenantRoles");
        return service.loadTenantRoles(request);
    }
}
