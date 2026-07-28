package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.RefundFlowCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class RefundFlowWorker implements WorkerMarker {
    private final RefundFlowCoordinator coordinator;

    public RefundFlowWorker(RefundFlowCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "one" -> coordinator.refundAgainstOrder(payload);
            case "two" -> coordinator.refundRemainingAmount(payload);
            case "three" -> coordinator.refundConvertedAmount(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "refund-flow";
    }
}
