package com.tradeflow.web.feature;

import com.tradeflow.application.feature.CouponPricingCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/coupon-pricing")
public final class CouponPricingController {
    private final CouponPricingCoordinator coordinator;

    public CouponPricingController(CouponPricingCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/1")
    public ResponseEntity<Object> calculateCombinedDiscount(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.calculateCombinedDiscount(request));
    }

    @PostMapping("/2")
    public ResponseEntity<Object> applyCouponRules(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.applyCouponRules(request));
    }

    @PostMapping("/3")
    public ResponseEntity<Object> loadCustomerPrice(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.loadCustomerPrice(request));
    }
}
