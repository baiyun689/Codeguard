package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class ProductCacheCoordinator {
    private final ProductCacheService service;
    private final AuditSink audit;
    private final TenantContext context;

    public ProductCacheCoordinator(
            ProductCacheService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object loadProduct(Map<String, String> request) {
        audit.record(context.tenantId(), "PRODUCT-CACHE", "loadProduct");
        return service.loadProduct(request);
    }

    public Object updateProduct(Map<String, String> request) {
        audit.record(context.tenantId(), "PRODUCT-CACHE", "updateProduct");
        return service.updateProduct(request);
    }

    public Object createProduct(Map<String, String> request) {
        audit.record(context.tenantId(), "PRODUCT-CACHE", "createProduct");
        return service.createProduct(request);
    }
}
