package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.CouponPricingCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class CouponPricingWorker implements WorkerMarker {
    private final CouponPricingCoordinator coordinator;

    public CouponPricingWorker(CouponPricingCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "calculate-combined-discount" ->
                    coordinator.calculateCombinedDiscount(payload);
            case "apply-coupon-rules" ->
                    coordinator.applyCouponRules(payload);
            case "load-customer-price" ->
                    coordinator.loadCustomerPrice(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "coupon-pricing";
    }
}
