package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class LoginRateLimitCoordinator {
    private final LoginRateLimitService service;
    private final AuditSink audit;
    private final TenantContext context;

    public LoginRateLimitCoordinator(
            LoginRateLimitService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object countByClientAddress(Map<String, String> request) {
        audit.record(context.tenantId(), "LOGIN-RATE-LIMIT", "countByClientAddress");
        return service.countByClientAddress(request);
    }

    public Object recordLoginFailure(Map<String, String> request) {
        audit.record(context.tenantId(), "LOGIN-RATE-LIMIT", "recordLoginFailure");
        return service.recordLoginFailure(request);
    }

    public Object checkRateLimit(Map<String, String> request) {
        audit.record(context.tenantId(), "LOGIN-RATE-LIMIT", "checkRateLimit");
        return service.checkRateLimit(request);
    }
}
