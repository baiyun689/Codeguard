package com.tradeflow.web.feature;

import com.tradeflow.application.feature.InventoryReservationCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/inventory-reservation")
public final class InventoryReservationController {
    private final InventoryReservationCoordinator coordinator;

    public InventoryReservationController(InventoryReservationCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/reserve-stock")
    public ResponseEntity<Object> reserveStock(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.reserveStock(request));
    }

    @PostMapping("/reserve-with-local-lock")
    public ResponseEntity<Object> reserveWithLocalLock(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.reserveWithLocalLock(request));
    }

    @PostMapping("/release-expired-reservation")
    public ResponseEntity<Object> releaseExpiredReservation(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.releaseExpiredReservation(request));
    }
}
