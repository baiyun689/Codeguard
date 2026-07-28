package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.UserInvitationCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class UserInvitationWorker implements WorkerMarker {
    private final UserInvitationCoordinator coordinator;

    public UserInvitationWorker(UserInvitationCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "invite-tenant-member" ->
                    coordinator.inviteTenantMember(payload);
            case "invite-with-role-check" ->
                    coordinator.inviteWithRoleCheck(payload);
            case "send-invitation" ->
                    coordinator.sendInvitation(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "user-invitation";
    }
}
