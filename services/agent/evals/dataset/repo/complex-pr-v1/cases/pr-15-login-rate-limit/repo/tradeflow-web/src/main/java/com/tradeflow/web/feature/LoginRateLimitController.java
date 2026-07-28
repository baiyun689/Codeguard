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

    @PostMapping("/1")
    public ResponseEntity<Object> countByClientAddress(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.countByClientAddress(request));
    }

    @PostMapping("/2")
    public ResponseEntity<Object> recordLoginFailure(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.recordLoginFailure(request));
    }

    @PostMapping("/3")
    public ResponseEntity<Object> checkRateLimit(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.checkRateLimit(request));
    }
}
