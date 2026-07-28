package com.tradeflow.web.feature;

import com.tradeflow.application.feature.TenantConfigCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/tenant-config")
public final class TenantConfigController {
    private final TenantConfigCoordinator coordinator;

    public TenantConfigController(TenantConfigCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/load-tenant-secret")
    public ResponseEntity<Object> loadTenantSecret(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.loadTenantSecret(request));
    }

    @PostMapping("/reload-configuration")
    public ResponseEntity<Object> reloadConfiguration(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.reloadConfiguration(request));
    }

    @PostMapping("/publish-configuration")
    public ResponseEntity<Object> publishConfiguration(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.publishConfiguration(request));
    }
}
