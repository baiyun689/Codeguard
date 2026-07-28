package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class AdminReportCoordinator {
    private final AdminReportService service;
    private final AuditSink audit;
    private final TenantContext context;

    public AdminReportCoordinator(
            AdminReportService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object runScheduledExport(Map<String, String> request) {
        audit.record(context.tenantId(), "ADMIN-REPORT", "runScheduledExport");
        return service.runScheduledExport(request);
    }

    public Object renderFullReport(Map<String, String> request) {
        audit.record(context.tenantId(), "ADMIN-REPORT", "renderFullReport");
        return service.renderFullReport(request);
    }

    public Object renderReportRow(Map<String, String> request) {
        audit.record(context.tenantId(), "ADMIN-REPORT", "renderReportRow");
        return service.renderReportRow(request);
    }
}
