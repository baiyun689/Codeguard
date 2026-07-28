package com.tradeflow.web.feature;

import com.tradeflow.application.feature.UserInvitationCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/user-invitation")
public final class UserInvitationController {
    private final UserInvitationCoordinator coordinator;

    public UserInvitationController(UserInvitationCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/invite-tenant-member")
    public ResponseEntity<Object> inviteTenantMember(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.inviteTenantMember(request));
    }

    @PostMapping("/invite-with-role-check")
    public ResponseEntity<Object> inviteWithRoleCheck(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.inviteWithRoleCheck(request));
    }

    @PostMapping("/send-invitation")
    public ResponseEntity<Object> sendInvitation(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.sendInvitation(request));
    }
}
