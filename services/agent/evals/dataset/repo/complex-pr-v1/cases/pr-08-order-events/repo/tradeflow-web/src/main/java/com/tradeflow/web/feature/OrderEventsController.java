package com.tradeflow.web.feature;

import com.tradeflow.application.feature.OrderEventsCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/order-events")
public final class OrderEventsController {
    private final OrderEventsCoordinator coordinator;

    public OrderEventsController(OrderEventsCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/1")
    public ResponseEntity<Object> changeOrderStatus(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.changeOrderStatus(request));
    }

    @PostMapping("/2")
    public ResponseEntity<Object> consumeOrderEvent(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.consumeOrderEvent(request));
    }

    @PostMapping("/3")
    public ResponseEntity<Object> applyVersionedEvent(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.applyVersionedEvent(request));
    }
}
