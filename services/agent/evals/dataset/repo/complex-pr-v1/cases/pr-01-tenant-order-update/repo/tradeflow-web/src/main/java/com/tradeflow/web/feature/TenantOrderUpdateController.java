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

    @PostMapping("/update-order")
    public ResponseEntity<Object> updateOrder(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.updateOrder(request));
    }

    @PostMapping("/apply-order-changes")
    public ResponseEntity<Object> applyOrderChanges(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.applyOrderChanges(request));
    }

    @PostMapping("/record-order-update")
    public ResponseEntity<Object> recordOrderUpdate(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.recordOrderUpdate(request));
    }
}
