package com.tradeflow.web.feature;

import com.tradeflow.application.feature.RefundFlowCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/refund-flow")
public final class RefundFlowController {
    private final RefundFlowCoordinator coordinator;

    public RefundFlowController(RefundFlowCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/1")
    public ResponseEntity<Object> refundAgainstOrder(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.refundAgainstOrder(request));
    }

    @PostMapping("/2")
    public ResponseEntity<Object> refundRemainingAmount(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.refundRemainingAmount(request));
    }

    @PostMapping("/3")
    public ResponseEntity<Object> refundConvertedAmount(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.refundConvertedAmount(request));
    }
}
