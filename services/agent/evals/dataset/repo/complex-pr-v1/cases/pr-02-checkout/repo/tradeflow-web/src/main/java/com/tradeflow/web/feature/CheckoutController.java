package com.tradeflow.web.feature;

import com.tradeflow.application.feature.CheckoutCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/checkout")
public final class CheckoutController {
    private final CheckoutCoordinator coordinator;

    public CheckoutController(CheckoutCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/1")
    public ResponseEntity<Object> uncompensatedCharge(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.uncompensatedCharge(request));
    }

    @PostMapping("/2")
    public ResponseEntity<Object> localTransaction(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.localTransaction(request));
    }

    @PostMapping("/3")
    public ResponseEntity<Object> sharedIdempotency(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.sharedIdempotency(request));
    }
}
