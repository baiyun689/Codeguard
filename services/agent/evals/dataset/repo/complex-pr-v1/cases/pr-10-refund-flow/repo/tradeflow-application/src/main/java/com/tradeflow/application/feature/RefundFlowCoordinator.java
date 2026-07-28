package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class RefundFlowCoordinator {
    private final RefundFlowService service;
    private final AuditSink audit;
    private final TenantContext context;

    public RefundFlowCoordinator(
            RefundFlowService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object refundAgainstOrder(Map<String, String> request) {
        audit.record(context.tenantId(), "REFUND-FLOW", "refundAgainstOrder");
        return service.refundAgainstOrder(request);
    }

    public Object refundRemainingAmount(Map<String, String> request) {
        audit.record(context.tenantId(), "REFUND-FLOW", "refundRemainingAmount");
        return service.refundRemainingAmount(request);
    }

    public Object refundConvertedAmount(Map<String, String> request) {
        audit.record(context.tenantId(), "REFUND-FLOW", "refundConvertedAmount");
        return service.refundConvertedAmount(request);
    }
}
