package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class FileExportCoordinator {
    private final FileExportService service;
    private final AuditSink audit;
    private final TenantContext context;

    public FileExportCoordinator(
            FileExportService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object openExport(Map<String, String> request) {
        audit.record(context.tenantId(), "FILE-EXPORT", "openExport");
        return service.openExport(request);
    }

    public Object openOwnedExport(Map<String, String> request) {
        audit.record(context.tenantId(), "FILE-EXPORT", "openOwnedExport");
        return service.openOwnedExport(request);
    }

    public Object openVerifiedExport(Map<String, String> request) {
        audit.record(context.tenantId(), "FILE-EXPORT", "openVerifiedExport");
        return service.openVerifiedExport(request);
    }
}
