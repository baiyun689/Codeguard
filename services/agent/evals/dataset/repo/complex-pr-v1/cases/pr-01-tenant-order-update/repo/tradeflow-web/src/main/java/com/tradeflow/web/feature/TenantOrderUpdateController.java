package com.tradeflow.web.feature;

import com.tradeflow.application.feature.TenantOrderUpdateCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/tenant-order-update")
public final class TenantOrderUpdateController {
    private final TenantOrderUpdateCoordinator coordinator;

    public TenantOrderUpdateController(TenantOrderUpdateCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/1")
    public ResponseEntity<Object> tenantLookup(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.tenantLookup(request));
    }

    @PostMapping("/2")
    public ResponseEntity<Object> mutableProjection(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.mutableProjection(request));
    }

    @PostMapping("/3")
    public ResponseEntity<Object> prematureAudit(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.prematureAudit(request));
    }
}
