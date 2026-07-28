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
            case "one" -> coordinator.uncompensatedCharge(payload);
            case "two" -> coordinator.localTransaction(payload);
            case "three" -> coordinator.sharedIdempotency(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "checkout";
    }
}
