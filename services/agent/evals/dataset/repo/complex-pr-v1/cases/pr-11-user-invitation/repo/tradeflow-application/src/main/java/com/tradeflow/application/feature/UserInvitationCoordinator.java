package com.tradeflow.application.feature;

import com.tradeflow.application.port.AuditSink;
import com.tradeflow.application.security.TenantContext;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public final class UserInvitationCoordinator {
    private final UserInvitationService service;
    private final AuditSink audit;
    private final TenantContext context;

    public UserInvitationCoordinator(
            UserInvitationService service, AuditSink audit, TenantContext context) {
        this.service = service;
        this.audit = audit;
        this.context = context;
    }

    public Object inviteTenantMember(Map<String, String> request) {
        audit.record(context.tenantId(), "USER-INVITATION", "inviteTenantMember");
        return service.inviteTenantMember(request);
    }

    public Object inviteWithRoleCheck(Map<String, String> request) {
        audit.record(context.tenantId(), "USER-INVITATION", "inviteWithRoleCheck");
        return service.inviteWithRoleCheck(request);
    }

    public Object sendInvitation(Map<String, String> request) {
        audit.record(context.tenantId(), "USER-INVITATION", "sendInvitation");
        return service.sendInvitation(request);
    }
}
