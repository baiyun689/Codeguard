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

    @PostMapping("/place-order")
    public ResponseEntity<Object> placeOrder(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.placeOrder(request));
    }

    @PostMapping("/complete-checkout")
    public ResponseEntity<Object> completeCheckout(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.completeCheckout(request));
    }

    @PostMapping("/submit-payment")
    public ResponseEntity<Object> submitPayment(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.submitPayment(request));
    }
}
