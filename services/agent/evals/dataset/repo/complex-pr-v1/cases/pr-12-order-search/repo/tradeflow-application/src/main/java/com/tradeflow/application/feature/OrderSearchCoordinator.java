package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class OrderSearchCoordinator {
    private final OrderSearchService service;
    private final AuditSink audit;
    private final TenantContext context;

    public OrderSearchCoordinator(
            OrderSearchService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object searchWithSort(Map<String, String> request) {
        audit.record(context.tenantId(), "ORDER-SEARCH", "searchWithSort");
        return service.searchWithSort(request);
    }

    public Object searchPage(Map<String, String> request) {
        audit.record(context.tenantId(), "ORDER-SEARCH", "searchPage");
        return service.searchPage(request);
    }

    public Object searchWithFallback(Map<String, String> request) {
        audit.record(context.tenantId(), "ORDER-SEARCH", "searchWithFallback");
        return service.searchWithFallback(request);
    }
}
