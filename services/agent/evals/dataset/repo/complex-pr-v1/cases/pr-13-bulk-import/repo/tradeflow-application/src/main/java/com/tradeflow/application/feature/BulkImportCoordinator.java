package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class BulkImportCoordinator {
    private final BulkImportService service;
    private final AuditSink audit;
    private final TenantContext context;

    public BulkImportCoordinator(
            BulkImportService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object resolveArchiveEntry(Map<String, String> request) {
        audit.record(context.tenantId(), "BULK-IMPORT", "resolveArchiveEntry");
        return service.resolveArchiveEntry(request);
    }

    public Object importRows(Map<String, String> request) {
        audit.record(context.tenantId(), "BULK-IMPORT", "importRows");
        return service.importRows(request);
    }

    public Object renderImportError(Map<String, String> request) {
        audit.record(context.tenantId(), "BULK-IMPORT", "renderImportError");
        return service.renderImportError(request);
    }
}
