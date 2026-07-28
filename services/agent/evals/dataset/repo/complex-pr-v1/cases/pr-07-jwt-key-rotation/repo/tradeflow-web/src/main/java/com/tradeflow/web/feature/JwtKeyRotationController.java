package com.tradeflow.web.feature;

import com.tradeflow.application.feature.JwtKeyRotationCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/jwt-key-rotation")
public final class JwtKeyRotationController {
    private final JwtKeyRotationCoordinator coordinator;

    public JwtKeyRotationController(JwtKeyRotationCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/load-signing-key")
    public ResponseEntity<Object> loadSigningKey(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.loadSigningKey(request));
    }

    @PostMapping("/is-token-active")
    public ResponseEntity<Object> isTokenActive(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.isTokenActive(request));
    }

    @PostMapping("/load-tenant-roles")
    public ResponseEntity<Object> loadTenantRoles(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.loadTenantRoles(request));
    }
}
