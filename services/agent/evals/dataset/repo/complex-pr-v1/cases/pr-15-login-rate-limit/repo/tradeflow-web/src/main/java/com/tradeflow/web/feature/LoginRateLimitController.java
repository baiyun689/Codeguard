package com.tradeflow.web.feature;

import com.tradeflow.application.feature.LoginRateLimitCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/login-rate-limit")
public final class LoginRateLimitController {
    private final LoginRateLimitCoordinator coordinator;

    public LoginRateLimitController(LoginRateLimitCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/count-by-client-address")
    public ResponseEntity<Object> countByClientAddress(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.countByClientAddress(request));
    }

    @PostMapping("/record-login-failure")
    public ResponseEntity<Object> recordLoginFailure(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.recordLoginFailure(request));
    }

    @PostMapping("/check-rate-limit")
    public ResponseEntity<Object> checkRateLimit(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.checkRateLimit(request));
    }
}
