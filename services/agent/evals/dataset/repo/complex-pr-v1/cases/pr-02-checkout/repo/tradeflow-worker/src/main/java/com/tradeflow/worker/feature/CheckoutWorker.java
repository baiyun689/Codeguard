package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.CheckoutCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class CheckoutWorker implements WorkerMarker {
    private final CheckoutCoordinator coordinator;

    public CheckoutWorker(CheckoutCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "place-order" ->
                    coordinator.placeOrder(payload);
            case "complete-checkout" ->
                    coordinator.completeCheckout(payload);
            case "submit-payment" ->
                    coordinator.submitPayment(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "checkout";
    }
}
