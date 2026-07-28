package com.tradeflow.web.feature;

import com.tradeflow.application.feature.OrderStateMachineCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/order-state-machine")
public final class OrderStateMachineController {
    private final OrderStateMachineCoordinator coordinator;

    public OrderStateMachineController(OrderStateMachineCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/transition-order")
    public ResponseEntity<Object> transitionOrder(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.transitionOrder(request));
    }

    @PostMapping("/map-order-update")
    public ResponseEntity<Object> mapOrderUpdate(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.mapOrderUpdate(request));
    }

    @PostMapping("/publish-compensation")
    public ResponseEntity<Object> publishCompensation(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.publishCompensation(request));
    }
}
