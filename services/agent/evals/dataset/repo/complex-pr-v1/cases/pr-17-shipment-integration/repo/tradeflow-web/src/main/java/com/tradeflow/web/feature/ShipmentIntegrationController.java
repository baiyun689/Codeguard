package com.tradeflow.web.feature;

import com.tradeflow.application.feature.ShipmentIntegrationCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/shipment-integration")
public final class ShipmentIntegrationController {
    private final ShipmentIntegrationCoordinator coordinator;

    public ShipmentIntegrationController(ShipmentIntegrationCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/1")
    public ResponseEntity<Object> createShipment(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.createShipment(request));
    }

    @PostMapping("/2")
    public ResponseEntity<Object> retryShipment(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.retryShipment(request));
    }

    @PostMapping("/3")
    public ResponseEntity<Object> createShipmentWithTimeout(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.createShipmentWithTimeout(request));
    }
}
