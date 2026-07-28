package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class NotificationTemplateCoordinator {
    private final NotificationTemplateService service;
    private final AuditSink audit;
    private final TenantContext context;

    public NotificationTemplateCoordinator(
            NotificationTemplateService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object renderNotification(Map<String, String> request) {
        audit.record(context.tenantId(), "NOTIFICATION-TEMPLATE", "renderNotification");
        return service.renderNotification(request);
    }

    public Object unsubscribeRecipient(Map<String, String> request) {
        audit.record(context.tenantId(), "NOTIFICATION-TEMPLATE", "unsubscribeRecipient");
        return service.unsubscribeRecipient(request);
    }

    public Object sendNotification(Map<String, String> request) {
        audit.record(context.tenantId(), "NOTIFICATION-TEMPLATE", "sendNotification");
        return service.sendNotification(request);
    }
}
