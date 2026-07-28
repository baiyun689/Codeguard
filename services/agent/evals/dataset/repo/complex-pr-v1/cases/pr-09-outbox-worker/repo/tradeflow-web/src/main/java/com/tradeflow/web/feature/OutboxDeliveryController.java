package com.tradeflow.web.feature;

import com.tradeflow.application.feature.OutboxDeliveryCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/outbox-worker")
public final class OutboxDeliveryController {
    private final OutboxDeliveryCoordinator coordinator;

    public OutboxDeliveryController(OutboxDeliveryCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/1")
    public ResponseEntity<Object> deliverReadyEvents(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.deliverReadyEvents(request));
    }

    @PostMapping("/2")
    public ResponseEntity<Object> deliverOneEvent(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.deliverOneEvent(request));
    }

    @PostMapping("/3")
    public ResponseEntity<Object> scheduleRetry(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.scheduleRetry(request));
    }
}
