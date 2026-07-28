package com.tradeflow.web.feature;

import com.tradeflow.application.feature.PaymentWebhookCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/payment-webhook")
public final class PaymentWebhookController {
    private final PaymentWebhookCoordinator coordinator;

    public PaymentWebhookController(PaymentWebhookCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/verify-webhook-signature")
    public ResponseEntity<Object> verifyWebhookSignature(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.verifyWebhookSignature(request));
    }

    @PostMapping("/accept-webhook-event")
    public ResponseEntity<Object> acceptWebhookEvent(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.acceptWebhookEvent(request));
    }

    @PostMapping("/apply-payment-event")
    public ResponseEntity<Object> applyPaymentEvent(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.applyPaymentEvent(request));
    }
}
